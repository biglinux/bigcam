"""A built-in preset has to be different from the factory defaults.

The Smooth preset shipped setting exactly three controls:

    exposure_dynamic_framerate = 0
    auto_exposure              = 3
    white_balance_automatic    = 1

On an ASUS FHD webcam the driver's own defaults for those three are 0, 3 and
1.  Smooth was therefore byte-for-byte identical to Factory defaults, and
choosing it did nothing at all — the user reported the two looking the same,
and they were.

Two things guard against it now: Smooth carries an override that is not a
default, and a preset that still comes out equal to the defaults on some
other camera is not offered rather than presented as a choice that lies.
"""

from __future__ import annotations

import pytest

from constants import BackendType
from core.camera_backend import CameraControl, CameraInfo, ControlCategory, ControlType
from core.camera_profiles import (
    PRESET_FACTORY,
    PRESET_QUALITY,
    PRESET_SMOOTH,
    ensure_builtin_profiles,
    load_profile,
)


@pytest.fixture(autouse=True)
def _fresh_profile_dir(tmp_path, monkeypatch):
    """Each test starts with no profiles on disk.

    ensure_builtin_profiles leaves existing presets alone — they become
    ordinary, user-editable profiles once created — so a shared directory
    would make every test after the first a no-op.
    """
    from utils import xdg

    monkeypatch.setattr(xdg, "profiles_dir", lambda: str(tmp_path / "profiles"))


@pytest.fixture
def camera():
    return CameraInfo(
        id="v4l2:/dev/video0", name="ASUS FHD webcam",
        backend=BackendType.V4L2, device_path="/dev/video0",
    )


def _ctrl(cid, default, minimum=None, maximum=None):
    return CameraControl(
        id=cid, name=cid, category=ControlCategory.IMAGE,
        control_type=ControlType.INTEGER, value=default, default=default,
        minimum=minimum, maximum=maximum,
    )


# The real control set of the camera that exposed the bug.
ASUS = [
    _ctrl("brightness", 0, -64, 64),
    _ctrl("contrast", 32, 0, 64),
    _ctrl("saturation", 64, 0, 128),
    _ctrl("gamma", 100, 72, 500),
    _ctrl("gain", 0, 0, 100),
    _ctrl("sharpness", 3, 0, 6),
    _ctrl("backlight_compensation", 1, 0, 2),
    _ctrl("power_line_frequency", 2, 0, 2),
    _ctrl("white_balance_automatic", 1),
    _ctrl("auto_exposure", 3, 0, 3),
    _ctrl("exposure_dynamic_framerate", 0),
]


def _defaults(controls):
    return {c.id: int(c.default) for c in controls}


# -- the regression --------------------------------------------------------


def test_smooth_differs_from_the_factory_defaults(camera):
    ensure_builtin_profiles(camera, ASUS)
    smooth = load_profile(camera, PRESET_SMOOTH)
    assert smooth, "Smooth was not created at all"
    assert smooth != _defaults(ASUS), (
        "Smooth is identical to the driver defaults — selecting it does "
        "nothing, which is exactly what the user saw"
    )


def test_quality_differs_from_the_factory_defaults(camera):
    ensure_builtin_profiles(camera, ASUS)
    assert load_profile(camera, PRESET_QUALITY) != _defaults(ASUS)


def test_smooth_and_quality_are_not_the_same(camera):
    ensure_builtin_profiles(camera, ASUS)
    assert load_profile(camera, PRESET_SMOOTH) != load_profile(camera, PRESET_QUALITY)


def test_factory_is_exactly_the_defaults(camera):
    ensure_builtin_profiles(camera, ASUS)
    assert load_profile(camera, PRESET_FACTORY) == _defaults(ASUS)


# -- what each preset is for -----------------------------------------------


def test_smooth_locks_the_frame_rate(camera):
    ensure_builtin_profiles(camera, ASUS)
    assert load_profile(camera, PRESET_SMOOTH)["exposure_dynamic_framerate"] == 0


def test_quality_allows_a_longer_exposure(camera):
    ensure_builtin_profiles(camera, ASUS)
    assert load_profile(camera, PRESET_QUALITY)["exposure_dynamic_framerate"] == 1


def test_smooth_compensates_with_gamma_not_gain(camera):
    """Gain drains chroma on these sensors; gamma does not."""
    ensure_builtin_profiles(camera, ASUS)
    smooth = load_profile(camera, PRESET_SMOOTH)
    assert smooth["gamma"] > 100, "no brightness compensation for the short exposure"
    assert smooth["gain"] == 0


# -- the relative override has to stay inside the control's range ----------


def test_gamma_is_clamped_to_the_control_range(camera):
    narrow = [c for c in ASUS if c.id != "gamma"] + [_ctrl("gamma", 9, 1, 10)]
    ensure_builtin_profiles(camera, narrow)
    assert load_profile(camera, PRESET_SMOOTH)["gamma"] == 10


def test_a_camera_without_gamma_is_not_offered_smooth(camera):
    """Gamma is the only thing making Smooth differ from the defaults here.

    Without it the preset would set four controls to the values the driver
    already uses, so it is correctly withheld rather than shown as a choice
    that does nothing.
    """
    no_gamma = [c for c in ASUS if c.id != "gamma"]
    created = ensure_builtin_profiles(camera, no_gamma)
    assert PRESET_SMOOTH not in created
    assert load_profile(camera, PRESET_SMOOTH) == {}


# -- the general guard -----------------------------------------------------


def test_a_preset_equal_to_the_defaults_is_not_offered(camera):
    """A camera whose defaults happen to match a preset should not see it."""
    minimal = [_ctrl("auto_exposure", 3, 0, 3), _ctrl("white_balance_automatic", 1)]
    created = ensure_builtin_profiles(camera, minimal)
    assert PRESET_SMOOTH not in created
    assert PRESET_QUALITY not in created
    assert PRESET_FACTORY in created


def test_no_controls_creates_nothing(camera):
    assert ensure_builtin_profiles(camera, []) == []
