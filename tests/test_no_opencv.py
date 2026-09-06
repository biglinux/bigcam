"""BigCam must start and stream with OpenCV absent.

python-opencv is an optional dependency: it is what effects, recording and
the phone camera decode with, but a plain webcam preview goes through
GStreamer and needs none of it.  Every module therefore wraps `import cv2` in
a try/except and gates its use on _HAS_CV2 — a guard that is easy to add in
one place and forget in the next, and that nothing exercises, because the
development machine always has OpenCV installed.

These tests reimport the modules with cv2 made unimportable.
"""

from __future__ import annotations

import builtins
import importlib
import sys

import pytest

MODULES = [
    "core.effects",
    "core.video_recorder",
    "core.stream_engine",
    "ui.settings_page",
]


@pytest.fixture
def without_cv2(monkeypatch):
    """Make `import cv2` fail, as on a system without python-opencv."""
    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "cv2" or name.startswith("cv2."):
            raise ImportError("No module named 'cv2'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    for mod in (*MODULES, "cv2"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    yield
    # Restore the real modules for the rest of the session.
    monkeypatch.undo()
    for mod in MODULES:
        sys.modules.pop(mod, None)
        importlib.import_module(mod)


@pytest.mark.parametrize("name", MODULES)
def test_module_imports_without_opencv(name, without_cv2):
    mod = importlib.import_module(name)
    assert getattr(mod, "_HAS_CV2", False) is False


def test_effects_report_themselves_unavailable(without_cv2):
    effects = importlib.import_module("core.effects")
    pipeline = effects.EffectPipeline()
    assert pipeline.available is False
    assert pipeline.get_effects() == []
    assert pipeline.has_active_effects() is False


def test_effects_pass_frames_through_untouched(without_cv2):
    import numpy as np

    effects = importlib.import_module("core.effects")
    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    out = effects.EffectPipeline().apply(frame)
    assert out is frame or (out == frame).all()


def test_background_vcam_feeder_declines_to_start(without_cv2):
    se = importlib.import_module("core.stream_engine")
    feeder = se._BgVcamFeeder("/dev/video0", "/dev/video20", "Cam")
    assert feeder.start() is False, "spawned a thread that would import cv2"
