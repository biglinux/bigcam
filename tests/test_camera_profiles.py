"""Camera profiles, including the built-in Quality / Smooth presets.

There is no single best camera configuration — it depends on the light in the
room, and the trade-off is real.  Measured on an ASUS FHD webcam:

    exposure_dynamic_framerate=1   20 fps, correctly exposed in a dim room
    exposure_dynamic_framerate=0   30 fps, but badly underexposed without light

Letting the sensor lengthen its exposure gathers actual photons, which beats
any amount of software correction; giving that up buys smoother motion.  Since
neither answer is right for everyone, the app ships both as named presets and
the user picks.  Guessing automatically is what produced a monochrome, milky
picture earlier in development.
"""

from __future__ import annotations

import pytest

from constants import BackendType
from core.camera_backend import CameraControl, CameraInfo
from constants import ControlCategory, ControlType
from core import camera_profiles as cp


@pytest.fixture(autouse=True)
def profiles_dir(tmp_path, monkeypatch):
    from utils import xdg

    monkeypatch.setattr(xdg, "profiles_dir", lambda: str(tmp_path))
    return tmp_path


@pytest.fixture
def camera():
    return CameraInfo(
        id="v4l2:/dev/video0", name="ASUS FHD webcam",
        backend=BackendType.V4L2, device_path="/dev/video0",
    )


def _ctrl(cid, value, minimum=0, maximum=100, default=0, flags=""):
    return CameraControl(
        id=cid, name=cid, category=ControlCategory.IMAGE,
        control_type=ControlType.INTEGER, value=value, default=default,
        minimum=minimum, maximum=maximum, flags=flags,
    )


@pytest.fixture
def controls():
    return [
        _ctrl("brightness", 0, -64, 64, 0),
        _ctrl("contrast", 32, 0, 64, 32),
        _ctrl("saturation", 64, 0, 128, 64),
        _ctrl("gamma", 100, 72, 500, 100),
        _ctrl("gain", 0, 0, 100, 0),
        _ctrl("auto_exposure", 3, 0, 3, 3),
        _ctrl("white_balance_automatic", 1, 0, 1, 1),
        _ctrl("exposure_dynamic_framerate", 1, 0, 1, 0),
        _ctrl("power_line_frequency", 2, 0, 2, 2),
        _ctrl("privacy", 0, 0, 1, 0, flags="read-only"),
    ]


# -- existing behaviour still works ---------------------------------------


def test_save_and_load_round_trip(camera, controls):
    cp.save_profile(camera, "mine", controls)
    loaded = cp.load_profile(camera, "mine")
    assert loaded["brightness"] == 0
    assert loaded["contrast"] == 32


def test_list_and_delete(camera, controls):
    cp.save_profile(camera, "mine", controls)
    assert "mine" in cp.list_profiles(camera)
    assert cp.delete_profile(camera, "mine") is True
    assert "mine" not in cp.list_profiles(camera)


def test_profiles_are_per_camera(camera, controls):
    other = CameraInfo(id="x", name="Other Cam", backend=BackendType.V4L2,
                       device_path="/dev/video1")
    cp.save_profile(camera, "mine", controls)
    assert cp.list_profiles(other) == []


# -- built-in presets ------------------------------------------------------


def test_builtin_presets_are_created_on_first_use(camera, controls):
    cp.ensure_builtin_profiles(camera, controls)
    names = cp.list_profiles(camera)
    for expected in (cp.PRESET_QUALITY, cp.PRESET_SMOOTH, cp.PRESET_FACTORY):
        assert expected in names


def test_quality_preset_lets_the_sensor_lengthen_exposure(camera, controls):
    """Gathering real light beats correcting a dark frame afterwards."""
    cp.ensure_builtin_profiles(camera, controls)
    values = cp.load_profile(camera, cp.PRESET_QUALITY)
    assert values["exposure_dynamic_framerate"] == 1
    assert values["auto_exposure"] == 3
    assert values["white_balance_automatic"] == 1


def test_smooth_preset_holds_the_framerate(camera, controls):
    cp.ensure_builtin_profiles(camera, controls)
    values = cp.load_profile(camera, cp.PRESET_SMOOTH)
    assert values["exposure_dynamic_framerate"] == 0


def test_factory_preset_uses_the_driver_defaults(camera, controls):
    cp.ensure_builtin_profiles(camera, controls)
    values = cp.load_profile(camera, cp.PRESET_FACTORY)
    assert values["contrast"] == 32
    assert values["exposure_dynamic_framerate"] == 0
    assert values["saturation"] == 64


def test_presets_skip_controls_the_camera_lacks(camera):
    """A control the device does not expose is simply left out of the preset.

    Quality still has to differ from the defaults, or it would be withheld
    as a no-op, so the one control it turns on is kept and the rest dropped.
    """
    minimal = [
        _ctrl("brightness", 0, -64, 64, 0),
        _ctrl("exposure_dynamic_framerate", 1, 0, 1, 0),
    ]
    cp.ensure_builtin_profiles(camera, minimal)
    values = cp.load_profile(camera, cp.PRESET_QUALITY)
    assert "gamma" not in values
    assert "brightness" in values
    assert values["exposure_dynamic_framerate"] == 1


def test_presets_never_include_read_only_controls(camera, controls):
    cp.ensure_builtin_profiles(camera, controls)
    for name in (cp.PRESET_QUALITY, cp.PRESET_SMOOTH, cp.PRESET_FACTORY):
        assert "privacy" not in cp.load_profile(camera, name)


def test_existing_user_edits_are_not_overwritten(camera, controls):
    cp.ensure_builtin_profiles(camera, controls)
    edited = dict(cp.load_profile(camera, cp.PRESET_QUALITY))
    edited["brightness"] = 42
    cp.write_profile(camera, cp.PRESET_QUALITY, edited)

    cp.ensure_builtin_profiles(camera, controls)  # second run
    assert cp.load_profile(camera, cp.PRESET_QUALITY)["brightness"] == 42


def test_builtin_names_are_reported():
    assert cp.builtin_names() == [
        cp.PRESET_QUALITY, cp.PRESET_SMOOTH, cp.PRESET_FACTORY
    ]


# -- applying --------------------------------------------------------------


def test_apply_writes_every_control_through_the_backend(camera, controls):
    applied: list[tuple[str, int]] = []

    class _Manager:
        def set_control(self, cam, name, value):
            applied.append((name, value))
            return True

    cp.ensure_builtin_profiles(camera, controls)
    count = cp.apply_profile(_Manager(), camera, cp.PRESET_SMOOTH)
    assert count > 0
    assert ("exposure_dynamic_framerate", 0) in applied


def test_apply_unknown_profile_is_a_noop(camera):
    class _Manager:
        def set_control(self, cam, name, value):
            raise AssertionError("should not be called")

    assert cp.apply_profile(_Manager(), camera, "nope") == 0


def test_apply_survives_a_control_the_camera_rejects(camera, controls):
    class _Manager:
        def set_control(self, cam, name, value):
            if name == "gain":
                raise OSError("device busy")
            return True

    cp.ensure_builtin_profiles(camera, controls)
    assert cp.apply_profile(_Manager(), camera, cp.PRESET_QUALITY) > 0


# -- remembering the choice ------------------------------------------------


def test_last_used_profile_is_remembered(camera, controls):
    cp.ensure_builtin_profiles(camera, controls)
    assert cp.last_profile(camera) is None
    cp.remember_profile(camera, cp.PRESET_SMOOTH)
    assert cp.last_profile(camera) == cp.PRESET_SMOOTH


def test_last_used_is_per_camera(camera, controls):
    other = CameraInfo(id="x", name="Other Cam", backend=BackendType.V4L2,
                       device_path="/dev/video1")
    cp.ensure_builtin_profiles(camera, controls)
    cp.remember_profile(camera, cp.PRESET_QUALITY)
    assert cp.last_profile(other) is None


def test_forgetting_a_deleted_profile(camera, controls):
    cp.save_profile(camera, "mine", controls)
    cp.remember_profile(camera, "mine")
    cp.delete_profile(camera, "mine")
    assert cp.last_profile(camera) is None, "points at a profile that is gone"


# -- filename safety -------------------------------------------------------


def test_camera_and_profile_names_cannot_escape_the_directory(camera, controls):
    import os

    cp.save_profile(camera, "../../evil", controls)
    assert not os.path.exists("/tmp/evil.json")
    files = []
    for _root, _dirs, names in os.walk(cp.xdg.profiles_dir()):
        files += names
    assert all(".." not in f for f in files)


def test_corrupt_profile_returns_empty(camera, controls):
    path = cp.save_profile(camera, "mine", controls)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ broken")
    assert cp.load_profile(camera, "mine") == {}
