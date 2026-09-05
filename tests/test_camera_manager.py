"""CameraManager: detection orchestration, dedup, hotplug lifecycle."""

from __future__ import annotations

import threading

import pytest
from gi.repository import GLib

from constants import BackendType
from core.camera_backend import CameraBackend, CameraInfo
from core.camera_manager import CameraManager


class FakeBackend(CameraBackend):
    def __init__(self, btype, cameras, delay=0.0, boom=False):
        self._type = btype
        self._cameras = cameras
        self._delay = delay
        self._boom = boom

    def get_backend_type(self):
        return self._type

    def is_available(self):
        return True

    def detect_cameras(self):
        if self._delay:
            threading.Event().wait(self._delay)
        if self._boom:
            raise RuntimeError("backend exploded")
        return list(self._cameras)

    def get_controls(self, camera):
        return []

    def set_control(self, camera, control_id, value):
        return True

    def get_gst_source(self, camera, fmt=None, prefer_v4l2=False):
        return "fakesrc"

    def can_capture_photo(self):
        return True

    def capture_photo(self, camera, output_path):
        return True


def _cam(cid, name, backend):
    return CameraInfo(id=cid, name=name, backend=backend, device_path=f"/dev/{cid}")


def _drain_main_loop(iterations: int = 400) -> None:
    ctx = GLib.MainContext.default()
    for _ in range(iterations):
        if not ctx.pending():
            threading.Event().wait(0.005)
        while ctx.pending():
            ctx.iteration(False)


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(CameraManager, "_register_backends", lambda self: None)
    return CameraManager()


def _run_detection(manager, timeout=5.0):
    done = threading.Event()
    emissions = {"n": 0}

    def _on_changed(_m):
        emissions["n"] += 1
        done.set()

    manager.connect("cameras-changed", _on_changed)
    manager.detect_cameras_async()

    deadline = threading.Event()
    ctx = GLib.MainContext.default()
    waited = 0.0
    while waited < timeout and not (done.is_set() and not manager._detecting):
        while ctx.pending():
            ctx.iteration(False)
        deadline.wait(0.01)
        waited += 0.01
    _drain_main_loop(50)
    return emissions


# -- happy path ------------------------------------------------------------


def test_detects_across_backends(manager):
    manager._backends = [
        FakeBackend(BackendType.V4L2, [_cam("v1", "Webcam A", BackendType.V4L2)]),
        FakeBackend(BackendType.LIBCAMERA,
                    [_cam("l1", "CSI Sensor", BackendType.LIBCAMERA)]),
    ]
    _run_detection(manager)
    assert {c.id for c in manager.cameras} == {"v1", "l1"}


def test_duplicate_names_resolve_to_highest_priority_backend(manager):
    manager._backends = [
        FakeBackend(BackendType.PIPEWIRE,
                    [_cam("p1", "HD Pro Webcam C920", BackendType.PIPEWIRE)]),
        FakeBackend(BackendType.V4L2,
                    [_cam("v1", "HD Pro Webcam C920", BackendType.V4L2)]),
    ]
    _run_detection(manager)
    cams = manager.cameras
    assert len(cams) == 1
    assert cams[0].backend is BackendType.V4L2


def test_backend_exception_does_not_abort_detection(manager):
    manager._backends = [
        FakeBackend(BackendType.GPHOTO2, [], boom=True),
        FakeBackend(BackendType.V4L2, [_cam("v1", "Webcam", BackendType.V4L2)]),
    ]
    _run_detection(manager)
    assert {c.id for c in manager.cameras} == {"v1"}


# -- the "no backend available" dead-end ----------------------------------


def test_no_backends_still_completes_and_emits(manager):
    """With zero scannable backends the UI must still be told 'no cameras'."""
    manager._backends = []
    emissions = _run_detection(manager, timeout=3.0)
    assert manager._detecting is False, "detection flag stuck — UI hangs forever"
    assert emissions["n"] >= 1, "no cameras-changed emitted; UI stays on 'detecting'"
    assert manager.cameras == []


def test_only_ip_backend_is_not_scanned(manager):
    from core.backends.ip_backend import IPBackend

    manager._backends = [IPBackend()]
    emissions = _run_detection(manager, timeout=3.0)
    assert manager._detecting is False
    assert emissions["n"] >= 1


# -- partial results should not thrash the UI -----------------------------


def test_slow_backend_does_not_cause_extra_emissions(manager):
    """One detection run must produce exactly one cameras-changed."""
    manager._backends = [
        FakeBackend(BackendType.V4L2,
                    [_cam("v1", "Fast Cam", BackendType.V4L2)]),
        FakeBackend(BackendType.GPHOTO2,
                    [_cam("g1", "Slow DSLR", BackendType.GPHOTO2)], delay=0.25),
    ]
    emissions = _run_detection(manager, timeout=5.0)
    assert {c.id for c in manager.cameras} == {"v1", "g1"}
    assert emissions["n"] == 1, (
        f"emitted {emissions['n']} times — partial results reset the UI selection"
    )


# -- manual cameras survive rescans ---------------------------------------


def test_phone_and_ip_cameras_survive_a_rescan(manager):
    manager._backends = [
        FakeBackend(BackendType.V4L2, [_cam("v1", "Webcam", BackendType.V4L2)]),
    ]
    manager.add_phone_camera(
        CameraInfo(id="phone:websocket", name="BigCam Phone",
                   backend=BackendType.PHONE, device_path="websocket")
    )
    _run_detection(manager)
    assert "phone:websocket" in {c.id for c in manager.cameras}


def test_remove_phone_camera(manager):
    manager.add_phone_camera(
        CameraInfo(id="phone:websocket", name="p",
                   backend=BackendType.PHONE, device_path="ws")
    )
    manager.remove_phone_camera()
    assert manager.cameras == []


# -- hotplug lifecycle -----------------------------------------------------


def test_stop_hotplug_is_idempotent_and_joins(manager, monkeypatch):
    monkeypatch.setattr(manager, "_snapshot_device_state", lambda: None)
    manager.start_hotplug(interval_ms=50)
    assert manager._poll_thread is not None
    manager.stop_hotplug()
    assert manager._poll_thread is None
    manager.stop_hotplug()  # second call must not raise


def test_stop_hotplug_terminates_the_poll_thread(manager, monkeypatch):
    monkeypatch.setattr(manager, "_snapshot_device_state", lambda: None)
    manager.start_hotplug(interval_ms=50)
    thread = manager._poll_thread
    manager.stop_hotplug()
    thread.join(timeout=3)
    assert not thread.is_alive(), "hotplug poll thread outlived stop_hotplug()"
