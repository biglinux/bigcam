"""The enhancement chain must actually run on frames the engine processes.

Every earlier measurement called AutoEnhancer and TemporalDenoiser directly.
They were correct and meant nothing: in the running application the objects
were never constructed, so the correction was silently skipped and the picture
was unchanged.

The gap: the setting can already be on at startup, in which case no toggle
fires, and construction lived inside the toggle.  These tests exercise
_apply_frame_processing — the method the GStreamer probe actually calls — so a
regression there fails here instead of shipping.
"""

from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from core.camera_manager import CameraManager  # noqa: E402
from core.stream_engine import StreamEngine  # noqa: E402


class _Settings:
    def __init__(self, data):
        self._data = dict(data)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value


def _engine(auto_enhance):
    mgr = CameraManager.__new__(CameraManager)
    eng = StreamEngine.__new__(StreamEngine)
    StreamEngine.__init__(eng, mgr, _Settings({"auto-enhance": auto_enhance}))
    return eng


def _dark_frame(value=25, h=180, w=240, seed=0):
    rng = np.random.default_rng(seed)
    return np.clip(rng.normal(0, 5, (h, w, 3)) + value, 0, 255).astype(np.uint8)


def _luma(frame):
    return float(np.median(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)))


# -- construction ----------------------------------------------------------


def test_enhancer_exists_when_the_setting_is_already_on():
    """The regression: a persisted setting never fired a toggle."""
    eng = _engine(True)
    assert eng._enhancer is not None, "enhancer was never built"
    assert eng._denoiser is not None, "denoiser was never built"


def test_nothing_is_built_when_the_setting_is_off():
    eng = _engine(False)
    assert eng._enhancer is None
    assert eng._denoiser is None


def test_toggling_on_at_runtime_builds_the_chain():
    eng = _engine(False)
    eng.set_auto_enhance(True)
    assert eng._enhancer is not None
    assert eng._denoiser is not None


def test_toggling_on_when_already_on_still_leaves_it_usable():
    eng = _engine(True)
    eng.set_auto_enhance(True)
    assert eng._enhancer is not None


# -- the frame path the probe really calls --------------------------------


def test_frames_are_brightened_when_enabled():
    eng = _engine(True)
    dark = _dark_frame()
    for _ in range(50):
        out = eng._apply_frame_processing(dark.copy())
    assert _luma(out) > _luma(dark) + 25, (
        f"engine did not brighten: {_luma(dark):.0f} -> {_luma(out):.0f}"
    )


def test_frames_are_untouched_when_disabled():
    eng = _engine(False)
    dark = _dark_frame()
    out = eng._apply_frame_processing(dark.copy())
    assert _luma(out) == pytest.approx(_luma(dark), abs=1)


def test_disabling_at_runtime_stops_the_correction():
    eng = _engine(True)
    dark = _dark_frame()
    for _ in range(30):
        eng._apply_frame_processing(dark.copy())
    eng.set_auto_enhance(False)
    out = eng._apply_frame_processing(dark.copy())
    assert _luma(out) == pytest.approx(_luma(dark), abs=1)


def test_the_probe_considers_auto_enhance_to_be_work():
    """If it is not 'work', the fast path drops most frames unprocessed."""
    eng = _engine(True)
    assert eng._has_processing_work() is True

    off = _engine(False)
    assert off._has_processing_work() is False


def test_denoise_runs_before_the_enhancer():
    """Order matters: denoising after lifting would blur amplified noise."""
    eng = _engine(True)
    seen: list[str] = []
    real_denoise = eng._denoiser.process
    real_enhance = eng._enhancer.process
    eng._denoiser.process = lambda f: seen.append("denoise") or real_denoise(f)
    eng._enhancer.process = lambda f: seen.append("enhance") or real_enhance(f)

    eng._apply_frame_processing(_dark_frame())
    assert seen == ["denoise", "enhance"]


def test_reset_defaults_turns_the_correction_off_for_real():
    eng = _engine(True)
    dark = _dark_frame()
    for _ in range(20):
        eng._apply_frame_processing(dark.copy())
    eng.reset_image_defaults()
    out = eng._apply_frame_processing(dark.copy())
    assert _luma(out) == pytest.approx(_luma(dark), abs=1)
