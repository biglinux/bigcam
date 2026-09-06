"""Exposure metering.

A webcam frame is a person in front of a background, and the background is
usually the darkest thing in shot.  Metering on the whole frame therefore
reads far darker than the subject and over-brightens: on a real capture the
user judged well exposed, the global median was 30 while the face sat at 54.

Weighting the centre fixes that without needing face detection, which is
unreliable in exactly the low light where metering matters most.
"""

from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from core.auto_enhance import (  # noqa: E402
    AutoEnhancer,
    analyse_frame,
    metered_luma,
)


def _scene(subject=120, background=20, h=240, w=320, seed=0):
    """A bright subject on a dark background, like a lit person in a dim room."""
    rng = np.random.default_rng(seed)
    frame = np.full((h, w, 3), background, dtype=np.float32)
    y0, y1 = h // 4, 3 * h // 4
    x0, x1 = w // 3, 2 * w // 3
    frame[y0:y1, x0:x1] = subject
    frame += rng.normal(0, 4, frame.shape)
    return np.clip(frame, 0, 255).astype(np.uint8)


def _flat(value, h=240, w=320, seed=1, sigma=6):
    rng = np.random.default_rng(seed)
    return np.clip(rng.normal(0, sigma, (h, w, 3)) + value, 0, 255).astype(np.uint8)


# -- the metric itself -----------------------------------------------------


def test_metering_follows_the_subject_not_the_background():
    frame = _scene(subject=120, background=20)
    global_median = float(np.median(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)))
    metered = metered_luma(frame)
    assert metered > global_median + 15, (
        f"metering ignored the subject: global {global_median:.0f}, "
        f"metered {metered:.0f}"
    )


def test_metering_matches_the_median_on_a_uniform_frame():
    """With nothing to favour, centre weighting must not shift the reading."""
    frame = _flat(90)
    assert abs(metered_luma(frame) - 90) < 8


def test_metering_is_not_fooled_by_a_bright_corner():
    """A lamp in the corner must not make the subject read as bright."""
    frame = _flat(30)
    frame[:40, -40:] = 250
    assert metered_luma(frame) < 60


def test_metering_handles_degenerate_input():
    assert metered_luma(np.zeros((8, 8, 3), dtype=np.uint8)) == pytest.approx(0, abs=1)
    assert metered_luma(np.full((8, 8, 3), 255, dtype=np.uint8)) > 240


def test_analysis_exposes_the_metered_value():
    stats = analyse_frame(_scene())
    assert hasattr(stats, "metered_luma")
    assert stats.metered_luma > stats.median_luma


# -- effect on the correction ---------------------------------------------


def test_a_lit_subject_is_not_over_brightened():
    """The case the user judged good must land near the target, not past it.

    Levels taken from that capture: subject ~54, background ~20.
    """
    frame = _scene(subject=54, background=20)
    enh = AutoEnhancer()
    for _ in range(50):
        out = enh.process(frame)
    subject = out[60:180, 106:213]
    mean = float(cv2.cvtColor(subject, cv2.COLOR_BGR2GRAY).mean())
    assert 90 <= mean <= 165, f"subject landed at {mean:.0f}, want roughly 120"


def test_a_genuinely_dark_scene_is_lifted_further_than_before():
    """gamma was clamped at 0.45, leaving very dark rooms at ~77.

    Noise sigma 16 matches the contrast measured on the unlit capture: dim,
    but with enough detail to be worth recovering.
    """
    frame = _flat(26, sigma=16)
    enh = AutoEnhancer()
    for _ in range(60):
        out = enh.process(frame)
    lifted = float(np.median(cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)))
    assert lifted > 95, f"dark scene only reached {lifted:.0f}"


def test_lifting_does_not_blow_out_highlights():
    frame = _scene(subject=120, background=20)
    enh = AutoEnhancer()
    for _ in range(50):
        out = enh.process(frame)
    clipped = float((cv2.cvtColor(out, cv2.COLOR_BGR2GRAY) >= 250).mean())
    assert clipped < 0.02, f"{clipped*100:.1f}% of the frame clipped to white"


def test_featureless_frames_are_not_amplified_into_noise():
    """A frame with no information to recover should not be pushed hard.

    Amplifying it only amplifies sensor noise, which looks worse than dark.
    """
    almost_black = np.full((240, 320, 3), 4, dtype=np.uint8)
    enh = AutoEnhancer()
    for _ in range(40):
        out = enh.process(almost_black)
    lifted = float(np.median(cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)))
    assert lifted < 110, f"pure noise amplified to {lifted:.0f}"
