"""The OpenCV background feeder only accepts real V4L2 nodes.

_BgVcamFeeder opens its path with cv2.VideoCapture, which can only open a
device node.  A libcamera camera carries the libcamera camera id in
device_path instead — on a USB webcam that is an ACPI path:

    \\_SB_.PC00.XHCI.RHUB.HS07-7:1.0-3277:0018

Handing that to the feeder is not harmless.  It retries fifteen times with a
sleep between attempts, so start-up spends about three seconds emitting

    [ WARN:0@2,144] global cap.cpp:212 open VIDEOIO(V4L2): backend is
    generally available but can't be used to capture by name

before logging one failure and giving up.  libcamera and pipewire cameras
have a working GStreamer source; that is the path they should take.
"""

from __future__ import annotations

import pytest

from constants import BackendType
from core.camera_backend import CameraInfo
from core.camera_manager import CameraManager
from core.stream_engine import StreamEngine, _is_v4l2_node


@pytest.fixture
def engine(monkeypatch, settings):
    monkeypatch.setattr(CameraManager, "_register_backends", lambda self: None)
    eng = StreamEngine(CameraManager(), settings)
    eng._prefer_v4l2 = True
    return eng


def _camera(backend: BackendType, device_path: str) -> CameraInfo:
    return CameraInfo(
        id=f"{backend.name.lower()}:{device_path}", name="Cam",
        backend=backend, device_path=device_path,
    )


ACPI_ID = "\\_SB_.PC00.XHCI.RHUB.HS07-7:1.0-3277:0018"


# -- the predicate ---------------------------------------------------------


@pytest.mark.parametrize("path", ["/dev/video0", "/dev/video21", "/dev/video100"])
def test_device_nodes_are_accepted(path):
    assert _is_v4l2_node(path) is True


@pytest.mark.parametrize(
    "path",
    [
        ACPI_ID,
        "",
        "platform/soc/camera",
        "rtsp://cam/live",
        "/dev/dri/card0",
        "video0",
    ],
)
def test_everything_else_is_rejected(path):
    assert _is_v4l2_node(path) is False


# -- what the engine does with it ------------------------------------------


@pytest.fixture
def feeders_built(monkeypatch):
    """Record every _BgVcamFeeder the engine constructs."""
    from core import stream_engine

    built: list[str] = []

    class _Fake:
        def __init__(self, device_path, loopback, name):
            built.append(device_path)

        def start(self):
            return True

        def stop(self):
            pass

    monkeypatch.setattr(stream_engine, "_BgVcamFeeder", _Fake)
    return built


def test_libcamera_does_not_get_an_opencv_feeder(engine, feeders_built, monkeypatch):
    monkeypatch.setattr(engine, "_stop_bg_vcam", lambda cam_id: None)
    camera = _camera(BackendType.LIBCAMERA, ACPI_ID)
    engine._create_bg_vcam_pipeline(camera.id, camera, "/dev/video20")
    assert ACPI_ID not in feeders_built, (
        "handed a libcamera id to cv2.VideoCapture; it retries 15 times "
        "before failing"
    )


def test_v4l2_still_gets_an_opencv_feeder(engine, feeders_built, monkeypatch):
    monkeypatch.setattr(engine, "_stop_bg_vcam", lambda cam_id: None)
    camera = _camera(BackendType.V4L2, "/dev/video0")
    engine._create_bg_vcam_pipeline(camera.id, camera, "/dev/video20")
    assert feeders_built == ["/dev/video0"]


def test_libcamera_falls_through_to_gstreamer(engine, feeders_built, monkeypatch):
    """The point is not to skip the camera, but to use the right source."""
    monkeypatch.setattr(engine, "_stop_bg_vcam", lambda cam_id: None)
    reached = []
    monkeypatch.setattr(
        engine._manager, "get_backend",
        lambda backend: reached.append(backend) or None,
    )
    camera = _camera(BackendType.LIBCAMERA, ACPI_ID)
    engine._create_bg_vcam_pipeline(camera.id, camera, "/dev/video20")
    assert reached == [BackendType.LIBCAMERA], "did not try the GStreamer source"
