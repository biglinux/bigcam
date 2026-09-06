"""Restoring the picture to factory defaults.

Image state lives in three places — V4L2 controls on the device, geometry in
StreamEngine (zoom/pan/tilt/sharpness/mirror) and the software effect chain.
A user asking for "defaults" means all three, in one action.
"""

from __future__ import annotations

import pytest

from constants import BackendType
from core.camera_backend import CameraInfo
from core.camera_manager import CameraManager
from core.stream_engine import StreamEngine


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(CameraManager, "_register_backends", lambda self: None)
    return CameraManager()


@pytest.fixture
def engine(manager, settings):
    return StreamEngine(manager, settings)


@pytest.fixture
def dirty(engine):
    """An engine with every image knob moved away from its default."""
    engine.set_zoom(2.5)
    engine.set_pan(0.7)
    engine.set_tilt(-0.4)
    engine.set_sharpness(0.8)
    engine.mirror = True
    engine.effects.set_enabled("grayscale", True)
    engine.effects.set_param("brightness", "brightness", 55)
    return engine


# -- the API exists -------------------------------------------------------


def test_engine_exposes_a_single_reset(engine):
    assert hasattr(engine, "reset_image_defaults"), (
        "there is no single entry point to restore the picture"
    )


# -- geometry --------------------------------------------------------------


def test_reset_restores_geometry(dirty):
    dirty.reset_image_defaults()
    assert dirty._zoom_level == 1.0
    assert dirty._pan == 0.0
    assert dirty._tilt == 0.0
    assert dirty._sharpness == 0.0


def test_reset_clears_software_effects(dirty):
    dirty.reset_image_defaults()
    assert dirty.effects.has_active_effects() is False
    info = dirty.effects.get_effect("brightness")
    assert all(p.value == p.default for p in info.params)


def test_mirror_is_preserved_by_default(dirty):
    """Mirror is a viewing preference, not an image correction."""
    dirty.reset_image_defaults()
    assert dirty.mirror is True


def test_mirror_can_be_reset_explicitly(dirty):
    dirty.reset_image_defaults(include_mirror=True)
    assert dirty.mirror is False


# -- hardware controls -----------------------------------------------------


def test_reset_also_restores_v4l2_controls(engine, manager, monkeypatch):
    """Brightness/contrast live on the device and must go back too."""
    calls: list[tuple] = []
    cam = CameraInfo(id="v4l2:/dev/video0", name="Cam",
                     backend=BackendType.V4L2, device_path="/dev/video0")
    engine._current_camera = cam

    monkeypatch.setattr(
        manager, "reset_all_controls",
        lambda camera, controls: calls.append((camera.id, len(controls))),
    )
    monkeypatch.setattr(manager, "get_controls", lambda camera: ["a", "b", "c"])

    engine.reset_image_defaults()
    assert calls == [("v4l2:/dev/video0", 3)], (
        "device-side controls were not restored"
    )


def test_reset_without_a_camera_is_safe(engine):
    engine._current_camera = None
    engine.reset_image_defaults()  # must not raise


def test_reset_survives_a_backend_failure(engine, manager, monkeypatch):
    """A camera that rejects a control must not abort the whole reset."""
    cam = CameraInfo(id="x", name="Cam", backend=BackendType.V4L2,
                     device_path="/dev/video0")
    engine._current_camera = cam
    engine.set_zoom(3.0)
    monkeypatch.setattr(manager, "get_controls",
                        lambda camera: (_ for _ in ()).throw(OSError("device gone")))

    engine.reset_image_defaults()
    assert engine._zoom_level == 1.0, "software reset was skipped after a device error"


# -- idempotence -----------------------------------------------------------


def test_reset_is_idempotent(dirty):
    dirty.reset_image_defaults()
    dirty.reset_image_defaults()
    assert dirty._zoom_level == 1.0
    assert dirty.effects.has_active_effects() is False


def test_reset_turns_off_auto_enhance(dirty):
    """Auto-enhance is an image correction; defaults means off."""
    dirty.set_auto_enhance(True)
    dirty.reset_image_defaults()
    assert dirty.auto_enhance is False
