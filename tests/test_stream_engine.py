"""StreamEngine wiring that does not need a live pipeline."""

from __future__ import annotations

import inspect

import pytest

from core.camera_manager import CameraManager
from core.stream_engine import StreamEngine


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(CameraManager, "_register_backends", lambda self: None)
    return CameraManager()


# -- per-camera virtual camera opt-out ------------------------------------


def test_settings_are_injected_not_guessed(manager, settings):
    engine = StreamEngine(manager, settings)
    assert engine._settings is settings


def test_vcam_disabled_list_is_honoured(manager, settings):
    settings.set("vcam-disabled-cameras", ["v4l2:/dev/video0"])
    engine = StreamEngine(manager, settings)
    assert engine._vcam_disabled_for("v4l2:/dev/video0") is True
    assert engine._vcam_disabled_for("v4l2:/dev/video1") is False


def test_vcam_opt_out_defaults_to_enabled(manager, settings):
    engine = StreamEngine(manager, settings)
    assert engine._vcam_disabled_for("anything") is False


def test_engine_without_settings_does_not_crash(manager):
    engine = StreamEngine(manager)
    assert engine._vcam_disabled_for("x") is False


def test_no_hasattr_settings_guard_remains():
    """The old `hasattr(self, "_settings")` made the setting a silent no-op."""
    src = inspect.getsource(__import__("core.stream_engine", fromlist=["x"]))
    assert 'hasattr(self, "_settings")' not in src


def test_corrupt_disabled_list_is_ignored(manager, settings):
    settings._data["vcam-disabled-cameras"] = "not-a-list"
    engine = StreamEngine(manager, settings)
    assert engine._vcam_disabled_for("x") is False


# -- pipeline construction shape ------------------------------------------


def test_paintable_suffix_exposes_every_named_element():
    """flip/crop/tee are looked up by name at runtime; all must be present."""
    from core.stream_engine import _paintable_suffix

    suffix = _paintable_suffix()
    for element in ("videoflip name=flip", "videocrop name=crop", "tee name=t"):
        assert element in suffix, f"{element} missing — the feature using it breaks"


def test_every_preview_pipeline_uses_the_shared_suffix():
    """No pipeline variant may hand-roll its own tail.

    The PipeWire->v4l2src fallbacks previously omitted flip/crop, silently
    killing mirror and zoom/pan/tilt after a fallback.
    """
    from core import stream_engine

    src = inspect.getsource(stream_engine)
    body = src.split("def _paintable_suffix", 1)[1]
    body = body.split("def _find_device_users", 1)[1]
    assert "gtk4paintablesink sync=" not in body, (
        "a pipeline variant still builds its own suffix instead of calling "
        "_paintable_suffix()"
    )
    assert body.count("_paintable_suffix()") >= 2


def test_paintable_suffix_is_parseable_gstreamer():
    from gi.repository import Gst

    from core.stream_engine import _paintable_suffix

    if Gst.ElementFactory.find("gtk4paintablesink") is None:
        pytest.skip("gst-plugin-gtk4 not installed")
    pipeline = Gst.parse_launch(f"videotestsrc num-buffers=1 ! {_paintable_suffix()}")
    try:
        assert pipeline.get_by_name("flip") is not None
        assert pipeline.get_by_name("crop") is not None
        assert pipeline.get_by_name("t") is not None
    finally:
        pipeline.set_state(Gst.State.NULL)


# -- zoom / pan / tilt clamping -------------------------------------------


def test_zoom_is_clamped(manager, settings):
    engine = StreamEngine(manager, settings)
    engine.set_zoom(99.0)
    assert engine._zoom_level == 4.0
    engine.set_zoom(-5.0)
    assert engine._zoom_level == 1.0


def test_pan_tilt_are_clamped(manager, settings):
    engine = StreamEngine(manager, settings)
    engine.set_pan(9.0)
    engine.set_tilt(-9.0)
    assert engine._pan == 1.0
    assert engine._tilt == -1.0


def test_sharpness_is_clamped(manager, settings):
    engine = StreamEngine(manager, settings)
    engine.set_sharpness(5.0)
    assert engine._sharpness == 1.0
