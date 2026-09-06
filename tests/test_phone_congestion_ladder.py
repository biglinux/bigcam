"""The phone client's congestion ladder, executed rather than pattern-matched.

Adapting JPEG quality alone cannot recover an order of magnitude.  720p30 at
q0.75 is roughly 25-50 Mbps sustained; a busy 2.4 GHz link delivers a
fraction of that, and no quality setting closes the gap.  What was left was
the hard skip at bufferedAmount>128 KB — frames dropped at random, which is
what "trava bastante" over Wi-Fi looks like.

The ladder degrades in a defined order: quality first because it is cheapest
to give up, then frame rate, then resolution.  One counter drives all three
so they cannot fight each other, and recovery walks back down it.

These tests extract adapt() from the served page and run it under node with a
fake WebSocket, so they check what the phone will actually do.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import core.phone_camera as phone_camera

SOURCE = Path(phone_camera.__file__).read_text()

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


def _extract(name: str) -> str:
    """Pull one function's source out of the embedded page."""
    start = SOURCE.index(f"function {name}(")
    depth, i = 0, SOURCE.index("{", start)
    for j in range(i, len(SOURCE)):
        if SOURCE[j] == "{":
            depth += 1
        elif SOURCE[j] == "}":
            depth -= 1
            if depth == 0:
                return SOURCE[start : j + 1]
    raise AssertionError(f"unbalanced braces in {name}()")


HARNESS = """
%(adapt)s

let congestion=0, adaptiveQ=0.75, baseQ=0.75, rateDiv=1, scale=1;
let useWT=false, ws={bufferedAmount:0};
const CONGESTION_MAX=12;

const steps=[];
for(const buffered of BUFFERS){
  ws.bufferedAmount=buffered;
  adapt();
  steps.push({congestion, q:+adaptiveQ.toFixed(4), rateDiv, scale});
}
console.log(JSON.stringify(steps));
"""


def _run(buffers: list[int]) -> list[dict]:
    script = (
        f"const BUFFERS={json.dumps(buffers)};\n"
        + HARNESS % {"adapt": _extract("adapt")}
    )
    res = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=30
    )
    assert res.returncode == 0, f"the client JS does not run:\n{res.stderr}"
    return json.loads(res.stdout)


CONGESTED = 200_000     # above the 64 KB back-off threshold
CLEAR = 0               # below the 16 KB recovery threshold


# -- it degrades, and in the right order -----------------------------------


def test_quality_gives_way_first():
    steps = _run([CONGESTED])
    assert steps[0]["q"] < 0.75
    assert steps[0]["rateDiv"] == 1, "cut the frame rate before the quality"
    assert steps[0]["scale"] == 1, "cut the resolution before the quality"


def test_frame_rate_gives_way_before_resolution():
    steps = _run([CONGESTED] * 12)
    first_rate = next(i for i, s in enumerate(steps) if s["rateDiv"] > 1)
    first_scale = next(i for i, s in enumerate(steps) if s["scale"] < 1)
    assert first_rate < first_scale


def test_sustained_congestion_reaches_the_bottom():
    """The point of the fix: one step down was not enough."""
    final = _run([CONGESTED] * 20)[-1]
    assert final["q"] <= 0.45, f"quality stalled at {final['q']}"
    assert final["rateDiv"] == 2, "frame rate never halved"
    assert final["scale"] == 0.5, "resolution never reduced"


def test_degradation_is_monotone():
    steps = _run([CONGESTED] * 20)
    qs = [s["q"] for s in steps]
    assert qs == sorted(qs, reverse=True), "quality went back up under congestion"


# -- and it recovers -------------------------------------------------------


def test_a_clear_link_returns_to_full_quality():
    steps = _run([CONGESTED] * 20 + [CLEAR] * 30)
    final = steps[-1]
    assert final["congestion"] == 0
    assert final["q"] == pytest.approx(0.75)
    assert final["rateDiv"] == 1
    assert final["scale"] == 1


def test_recovery_is_gradual():
    """Snapping straight back to full rate just re-congests the link."""
    steps = _run([CONGESTED] * 20 + [CLEAR] * 30)
    after = [s["congestion"] for s in steps[20:]]
    assert after[0] > after[1] > after[2], "recovery is not stepwise"


def test_the_counter_is_bounded():
    steps = _run([CONGESTED] * 200)
    assert steps[-1]["congestion"] == 12, "counter is not clamped"


# -- degenerate input ------------------------------------------------------


def test_a_steady_clear_link_never_degrades():
    for step in _run([CLEAR] * 20):
        assert step == {"congestion": 0, "q": 0.75, "rateDiv": 1, "scale": 1}
