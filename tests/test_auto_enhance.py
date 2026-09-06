"""Automatic image enhancement.

Three classic, cheap corrections, applied only when the frame actually needs
them:

* **auto gamma** — pulls median luminance toward a mid target, fixing frames
  that are globally too dark or too bright;
* **CLAHE** on the L channel — recovers local contrast in flat/hazy frames
  without blowing out the rest;
* **gray-world white balance** — removes a colour cast.

The hard requirements are that a good frame is left alone, the correction is
stable frame to frame (no flicker), and analysis is cheap enough to run in the
preview path.
"""

from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from core.auto_enhance import AutoEnhancer, analyse_frame  # noqa: E402


def _frame(h=240, w=320, value=128, seed=0):
    """A textured frame centred on *value* — flat images defeat CLAHE."""
    rng = np.random.default_rng(seed)
    noise = rng.normal(0, 18, (h, w, 3))
    return np.clip(noise + value, 0, 255).astype(np.uint8)


def _tinted(base, b=1.0, g=1.0, r=1.0):
    out = base.astype(np.float32)
    out[:, :, 0] *= b
    out[:, :, 1] *= g
    out[:, :, 2] *= r
    return np.clip(out, 0, 255).astype(np.uint8)


def _median_luma(frame):
    return float(np.median(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)))


# -- analysis --------------------------------------------------------------


def test_analyse_reports_luma_and_cast():
    stats = analyse_frame(_frame(value=40))
    assert 0 <= stats.median_luma <= 255
    assert stats.median_luma < 80


def test_analyse_detects_a_colour_cast():
    neutral = analyse_frame(_frame(value=120))
    blue = analyse_frame(_tinted(_frame(value=120), b=1.6, r=0.7))
    assert blue.colour_cast > neutral.colour_cast


def test_analyse_is_cheap_on_a_1080p_frame():
    import time

    big = _frame(1080, 1920, value=110)
    t0 = time.perf_counter()
    for _ in range(5):
        analyse_frame(big)
    per_call_ms = (time.perf_counter() - t0) / 5 * 1000
    assert per_call_ms < 15, f"analysis costs {per_call_ms:.1f}ms per frame"


# -- correction ------------------------------------------------------------


def test_dark_frame_is_brightened():
    enh = AutoEnhancer()
    dark = _frame(value=45)
    out = enh.process(dark)
    assert _median_luma(out) > _median_luma(dark) + 15


def test_bright_frame_is_pulled_down():
    enh = AutoEnhancer()
    bright = _frame(value=215)
    out = enh.process(bright)
    assert _median_luma(out) < _median_luma(bright) - 10


def test_well_exposed_frame_is_left_alone():
    """No visible change on a frame that is already fine."""
    enh = AutoEnhancer()
    good = _frame(value=125)
    out = enh.process(good)
    assert abs(_median_luma(out) - _median_luma(good)) < 6


def test_colour_cast_is_reduced():
    enh = AutoEnhancer()
    tinted = _tinted(_frame(value=120), b=1.7, r=0.6)
    out = enh.process(tinted)

    def spread(f):
        means = [float(f[:, :, c].mean()) for c in range(3)]
        return max(means) - min(means)

    assert spread(out) < spread(tinted) * 0.7


def test_output_shape_and_dtype_are_preserved():
    enh = AutoEnhancer()
    src = _frame(value=70)
    out = enh.process(src)
    assert out.shape == src.shape
    assert out.dtype == np.uint8


def test_never_returns_the_input_buffer_mutated():
    enh = AutoEnhancer()
    src = _frame(value=50)
    original = src.copy()
    enh.process(src)
    assert np.array_equal(src, original), "process() mutated its input in place"


# -- stability (no flicker) ------------------------------------------------


def test_correction_is_stable_across_identical_frames():
    """The same scene must not oscillate between frames."""
    enh = AutoEnhancer()
    src = _frame(value=60)
    lumas = [_median_luma(enh.process(src)) for _ in range(30)]
    tail = lumas[10:]
    assert max(tail) - min(tail) < 3.0, f"output oscillates: {max(tail)-min(tail):.1f}"


def test_adaptation_is_gradual_not_instant():
    """A sudden lighting change should ease in, not snap."""
    enh = AutoEnhancer()
    for _ in range(20):
        enh.process(_frame(value=130))
    settled = enh.gain
    enh.process(_frame(value=30))
    assert abs(enh.gain - settled) < 1.0, "gain jumped in a single frame"


def test_converges_after_enough_frames():
    enh = AutoEnhancer()
    dark = _frame(value=40)
    for _ in range(60):
        out = enh.process(dark)
    assert _median_luma(out) > 85, "never reached a reasonable exposure"


def test_reset_clears_adaptation_state():
    enh = AutoEnhancer()
    for _ in range(30):
        enh.process(_frame(value=40))
    enh.reset()
    assert enh.gain == pytest.approx(1.0)


# -- robustness ------------------------------------------------------------


@pytest.mark.parametrize("value", [0, 255])
def test_degenerate_frames_do_not_crash(value):
    enh = AutoEnhancer()
    flat = np.full((64, 64, 3), value, dtype=np.uint8)
    out = enh.process(flat)
    assert out.shape == flat.shape
    assert np.isfinite(out).all()


def test_tiny_frame_is_handled():
    enh = AutoEnhancer()
    out = enh.process(np.zeros((2, 2, 3), dtype=np.uint8))
    assert out.shape == (2, 2, 3)


def test_grayscale_input_is_rejected_gracefully():
    enh = AutoEnhancer()
    gray = np.zeros((32, 32), dtype=np.uint8)
    out = enh.process(gray)
    assert out is gray, "unexpected shapes should pass through untouched"


# -- engine integration ----------------------------------------------------


def test_engine_exposes_the_toggle(settings):
    from core.camera_manager import CameraManager

    mgr = CameraManager.__new__(CameraManager)
    from core.stream_engine import StreamEngine

    eng = StreamEngine.__new__(StreamEngine)
    StreamEngine.__init__(eng, mgr, settings)
    assert eng.auto_enhance is False
    eng.set_auto_enhance(True)
    assert eng.auto_enhance is True


def test_toggle_is_persisted(settings):
    from core.camera_manager import CameraManager
    from core.stream_engine import StreamEngine

    mgr = CameraManager.__new__(CameraManager)
    eng = StreamEngine.__new__(StreamEngine)
    StreamEngine.__init__(eng, mgr, settings)
    eng.set_auto_enhance(True)
    assert settings.get("auto-enhance") is True
