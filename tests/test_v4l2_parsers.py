"""Pure-parsing tests for the V4L2 backend (no hardware, no subprocess)."""

from __future__ import annotations

import pytest

from constants import BackendType, ControlType
from core.backends.v4l2_backend import V4L2Backend

LIST_DEVICES = """\
Integrated Camera: Integrated C (usb-0000:00:14.0-8):
\t/dev/video0
\t/dev/video1
\t/dev/media0

BigCam Virtual 1 (platform:v4l2loopback-000):
\t/dev/video20

HD Pro Webcam C920 (usb-0000:00:14.0-2):
\t/dev/video2
\t/dev/video3
"""

FORMATS_EXT = """\
ioctl: VIDIOC_ENUM_FMT
\tType: Video Capture

\t[0]: 'MJPG' (Motion-JPEG, compressed)
\t\tSize: Discrete 1920x1080
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t\t\tInterval: Discrete 0.067s (15.000 fps)
\t\tSize: Discrete 1280x720
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t[1]: 'YUYV' (YUYV 4:2:2)
\t\tSize: Discrete 640x480
\t\t\tInterval: Discrete 0.033s (30.000 fps)
\t\tSize: Discrete 1280x720
\t\t\tInterval: Discrete 0.100s (10.000 fps)
"""

CTRLS_MENUS = """\
User Controls

                     brightness 0x00980900 (int)    : min=0 max=255 step=1 default=128 value=140
                       contrast 0x00980901 (int)    : min=0 max=255 step=1 default=128 value=128
        white_balance_automatic 0x0098090c (bool)   : default=1 value=1
           power_line_frequency 0x00980918 (menu)   : min=0 max=2 default=1 value=1
\t\t\t\t0: Disabled
\t\t\t\t1: 50 Hz
\t\t\t\t2: 60 Hz
          backlight_compensation 0x0098091c (int)    : min=0 max=2 step=1 default=1 value=1

Camera Controls

                  auto_exposure 0x009a0901 (menu)   : min=0 max=3 default=3 value=3
\t\t\t\t1: Manual Mode
\t\t\t\t3: Aperture Priority Mode
       exposure_time_absolute 0x009a0902 (int)    : min=3 max=2047 step=1 default=250 value=250 flags=inactive
"""


@pytest.fixture
def backend():
    return V4L2Backend()


# -- device listing --------------------------------------------------------


def test_parse_devices_skips_loopback(backend, monkeypatch):
    monkeypatch.setattr(backend, "_is_capture_device", lambda dev: True)
    monkeypatch.setattr(backend, "_get_formats", lambda dev: [])
    cams = backend._parse_devices(LIST_DEVICES)
    names = [c.name for c in cams]
    assert "BigCam Virtual 1" not in names
    assert len(cams) == 2
    assert all(c.backend is BackendType.V4L2 for c in cams)


def test_parse_devices_picks_a_capture_node_not_just_the_first(backend, monkeypatch):
    """A camera whose first node is metadata-only must still be detected."""
    # /dev/video0 is NOT a capture device, /dev/video1 is.
    monkeypatch.setattr(
        backend, "_is_capture_device", lambda dev: dev in ("/dev/video1", "/dev/video2")
    )
    monkeypatch.setattr(backend, "_get_formats", lambda dev: [])
    cams = backend._parse_devices(LIST_DEVICES)
    paths = {c.device_path for c in cams}
    assert "/dev/video1" in paths, "should fall through to the next capture node"
    assert "/dev/video2" in paths


def test_parse_devices_drops_camera_with_no_capture_node(backend, monkeypatch):
    monkeypatch.setattr(backend, "_is_capture_device", lambda dev: False)
    monkeypatch.setattr(backend, "_get_formats", lambda dev: [])
    assert backend._parse_devices(LIST_DEVICES) == []


# -- formats ---------------------------------------------------------------


def test_parse_formats_ext(backend):
    fmts = backend._parse_formats_ext(FORMATS_EXT)
    assert len(fmts) == 4
    mjpg = [f for f in fmts if f.pixel_format == "MJPG"]
    assert {(f.width, f.height) for f in mjpg} == {(1920, 1080), (1280, 720)}
    assert max(mjpg[0].fps) == 30.0


def test_pick_best_format_prefers_mjpeg_and_does_not_mutate(backend):
    from core.camera_backend import CameraInfo

    fmts = backend._parse_formats_ext(FORMATS_EXT)
    cam = CameraInfo(
        id="x", name="x", backend=BackendType.V4L2, device_path="/dev/video0",
        formats=fmts,
    )
    before = list(cam.formats)
    best = backend._pick_best_format(cam)
    assert best.pixel_format == "MJPG"
    assert (best.width, best.height) == (1920, 1080)
    assert cam.formats == before, "_pick_best_format must not reorder camera.formats"


def test_pick_best_format_caps_raw_to_640x480(backend):
    from core.camera_backend import CameraInfo, VideoFormat

    cam = CameraInfo(
        id="x", name="x", backend=BackendType.V4L2, device_path="/dev/video0",
        formats=[
            VideoFormat(1920, 1080, [30.0], "YUYV"),
            VideoFormat(640, 480, [30.0], "YUYV"),
        ],
    )
    best = backend._pick_best_format(cam)
    assert (best.width, best.height) == (640, 480)


# -- controls --------------------------------------------------------------


def test_parse_controls_types_and_hidden(backend):
    ctrls = backend._parse_controls(CTRLS_MENUS)
    by_id = {c.id: c for c in ctrls}

    assert "backlight_compensation" not in by_id, "hidden control leaked into the UI"

    assert by_id["brightness"].control_type is ControlType.INTEGER
    assert by_id["brightness"].value == 140
    assert by_id["brightness"].default == 128
    assert by_id["brightness"].minimum == 0
    assert by_id["brightness"].maximum == 255

    assert by_id["white_balance_automatic"].control_type is ControlType.BOOLEAN
    assert by_id["exposure_time_absolute"].flags == "inactive"


def test_parse_controls_menu_choices_use_real_v4l2_indices(backend):
    ctrls = backend._parse_controls(CTRLS_MENUS)
    by_id = {c.id: c for c in ctrls}

    plf = by_id["power_line_frequency"]
    assert plf.choices == ["Disabled", "50 Hz", "60 Hz"]
    assert plf.choice_values == [0, 1, 2]

    # auto_exposure has a *sparse* menu (1 and 3) — indices must not be 0..n
    ae = by_id["auto_exposure"]
    assert ae.choice_values == [1, 3]
    assert ae.choices == ["Manual Mode", "Aperture Priority Mode"]


def test_hidden_control_does_not_swallow_following_menu_entries(backend):
    """A hidden control must not become last_ctrl_id and eat menu lines."""
    ctrls = backend._parse_controls(CTRLS_MENUS)
    by_id = {c.id: c for c in ctrls}
    # power_line_frequency comes right before backlight_compensation;
    # auto_exposure comes right after.  Both must keep their own choices.
    assert by_id["power_line_frequency"].choices is not None
    assert by_id["auto_exposure"].choices is not None


def test_reset_all_controls_skips_inactive_and_readonly(backend, monkeypatch):
    from core.camera_backend import CameraInfo

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        backend, "set_control",
        lambda cam, cid, val: calls.append((cid, val)) or True,
    )
    cam = CameraInfo(id="x", name="x", backend=BackendType.V4L2,
                     device_path="/dev/video0")
    ctrls = backend._parse_controls(CTRLS_MENUS)
    backend.reset_all_controls(cam, ctrls)

    ids = [cid for cid, _ in calls]
    assert "exposure_time_absolute" not in ids
    assert ("brightness", 128) in calls
