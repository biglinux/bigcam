"""Motion-adaptive temporal denoising.

Averaging successive frames is what phone cameras do to beat a small sensor,
and it works because sensor noise is random while the scene is not.  Measured
on this webcam, stacking 8 frames cut temporal noise by 3.9x.

Two things make a naive implementation unusable in a live preview: buffering N
frames adds N/fps of latency (267 ms at 8 frames), and anything that moves
smears.  So this is a *recursive* filter — one running average, no buffer —
whose blend factor is chosen per pixel from how much that pixel changed.
Static background gets heavily averaged; a moving hand passes straight
through.
"""

from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from core.temporal_denoise import TemporalDenoiser  # noqa: E402


def _noisy(value=60, sigma=4, h=120, w=160, seed=None):
    """Sigma 4 is around what this class of sensor produces; the measured
    frame-to-frame noise on the real camera was 1.6, i.e. sigma ~1.1."""
    rng = np.random.default_rng(seed)
    return np.clip(
        rng.normal(0, sigma, (h, w, 3)) + value, 0, 255
    ).astype(np.uint8)


def _noise_level(frames):
    """Temporal standard deviation between successive frames."""
    f = [x.astype(np.float32) for x in frames]
    return float(np.median([np.std(f[i + 1] - f[i]) for i in range(len(f) - 1)]))


# -- noise reduction on a static scene ------------------------------------


def test_static_scene_noise_is_reduced():
    den = TemporalDenoiser()
    raw = [_noisy(seed=i) for i in range(24)]
    out = [den.process(f) for f in raw]
    before = _noise_level(raw[8:])
    after = _noise_level(out[8:])
    assert after < before * 0.6, (
        f"noise only fell from {before:.2f} to {after:.2f}"
    )


def test_converges_within_about_a_second():
    """At 30 fps the filter should be settled well inside a second."""
    den = TemporalDenoiser()
    raw = [_noisy(seed=i) for i in range(40)]
    out = [den.process(f) for f in raw]
    assert _noise_level(out[15:25]) < _noise_level(raw[15:25]) * 0.7


def test_mean_brightness_is_preserved():
    den = TemporalDenoiser()
    for i in range(25):
        out = den.process(_noisy(value=60, seed=i))
    assert abs(float(out.mean()) - 60) < 4, "averaging shifted the exposure"


# -- motion must not smear -------------------------------------------------


def test_moving_region_is_not_ghosted():
    """A bright object that appears must show up immediately, not fade in."""
    den = TemporalDenoiser()
    for i in range(20):
        den.process(_noisy(value=60, seed=i))

    moved = _noisy(value=60, seed=99)
    moved[40:80, 60:100] = 220
    out = den.process(moved)

    patch = float(out[40:80, 60:100].mean())
    assert patch > 180, f"moving region ghosted: {patch:.0f} instead of ~220"


def test_static_area_still_smoothed_while_something_else_moves():
    den = TemporalDenoiser()
    for i in range(20):
        den.process(_noisy(value=60, seed=i))

    outs = []
    for i in range(8):
        frame = _noisy(value=60, seed=200 + i)
        frame[40:80, 60:100] = 220 if i % 2 else 40   # flickering region
        outs.append(den.process(frame))

    static = [o[:30, :30] for o in outs]
    assert _noise_level(static) < 8.0, "static corner was not smoothed"


def test_scene_cut_is_followed_immediately():
    """Switching cameras or lights must not leave the old scene behind."""
    den = TemporalDenoiser()
    for i in range(20):
        den.process(_noisy(value=40, seed=i))
    out = den.process(np.full((120, 160, 3), 200, dtype=np.uint8))
    assert float(out.mean()) > 170, "held onto the previous scene"


# -- lifecycle -------------------------------------------------------------


def test_first_frame_passes_through_unchanged():
    den = TemporalDenoiser()
    frame = _noisy(seed=1)
    out = den.process(frame)
    assert np.array_equal(out, frame)


def test_input_is_never_mutated():
    den = TemporalDenoiser()
    frame = _noisy(seed=2)
    original = frame.copy()
    den.process(frame)
    den.process(frame)
    assert np.array_equal(frame, original)


def test_resolution_change_resets_cleanly():
    den = TemporalDenoiser()
    for i in range(10):
        den.process(_noisy(h=120, w=160, seed=i))
    out = den.process(_noisy(h=240, w=320, seed=99))
    assert out.shape == (240, 320, 3)


def test_reset_clears_history():
    den = TemporalDenoiser()
    for i in range(15):
        den.process(_noisy(value=40, seed=i))
    den.reset()
    bright = np.full((120, 160, 3), 200, dtype=np.uint8)
    assert np.array_equal(den.process(bright), bright)


def test_strength_zero_is_a_passthrough():
    den = TemporalDenoiser(strength=0.0)
    frames = [_noisy(seed=i) for i in range(6)]
    for f in frames:
        out = den.process(f)
    assert np.array_equal(out, frames[-1])


def test_output_dtype_and_shape():
    den = TemporalDenoiser()
    for i in range(5):
        out = den.process(_noisy(seed=i))
    assert out.dtype == np.uint8
    assert out.shape == (120, 160, 3)


def test_non_image_input_passes_through():
    den = TemporalDenoiser()
    gray = np.zeros((32, 32), dtype=np.uint8)
    assert den.process(gray) is gray


# -- cost ------------------------------------------------------------------


def test_cost_is_affordable_at_1080p():
    import time

    den = TemporalDenoiser()
    frame = _noisy(h=1080, w=1920, seed=1)
    for _ in range(4):
        den.process(frame)
    t0 = time.perf_counter()
    for _ in range(15):
        den.process(frame)
    ms = (time.perf_counter() - t0) / 15 * 1000
    assert ms < 18.0, f"{ms:.1f} ms per frame at 1080p is too slow for preview"
