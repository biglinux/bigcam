"""Virtual-camera allocation retries must give up.

Both retry paths rescheduled themselves every two seconds with no counter.  A
machine where allocation can never succeed — v4l2loopback absent, the device
pool exhausted, the user declining the polkit prompt — kept a timer firing for
the life of the process, each firing shelling out to v4l2-ctl and modprobe.
"""

from __future__ import annotations

import pytest

from constants import BackendType
from core.camera_backend import CameraInfo
from core.camera_manager import CameraManager
from core.stream_engine import StreamEngine
from core.virtual_camera import VirtualCamera


@pytest.fixture
def engine(monkeypatch, settings):
    monkeypatch.setattr(CameraManager, "_register_backends", lambda self: None)
    return StreamEngine(CameraManager(), settings)


@pytest.fixture
def camera():
    return CameraInfo(
        id="v4l2:/dev/video0", name="Cam",
        backend=BackendType.V4L2, device_path="/dev/video0",
    )


@pytest.fixture
def never_allocates(monkeypatch):
    """A machine where a loopback device can never be obtained."""
    monkeypatch.setattr(VirtualCamera, "is_enabled", classmethod(lambda cls: True))
    monkeypatch.setattr(
        VirtualCamera, "ensure_ready",
        classmethod(lambda cls, card_label=None, camera_id="": ""),
    )
    monkeypatch.setattr(
        VirtualCamera, "get_device_for_camera", classmethod(lambda cls, cid: "")
    )


@pytest.fixture
def captured_timers(monkeypatch):
    """Record scheduled retries and run them synchronously."""
    from core import stream_engine

    scheduled: list[tuple] = []

    def _timeout_add(ms, fn, *args):
        scheduled.append((ms, fn, args))
        return len(scheduled)

    monkeypatch.setattr(stream_engine.GLib, "timeout_add", _timeout_add)
    return scheduled


def _drain(scheduled, limit=100):
    """Run queued retries until they stop rescheduling, or *limit* rounds."""
    rounds = 0
    while scheduled and rounds < limit:
        _ms, fn, args = scheduled.pop(0)
        fn(*args)
        rounds += 1
    return rounds


# -- ensure_bg_vcam --------------------------------------------------------


def test_background_vcam_retries_are_bounded(
    engine, camera, never_allocates, captured_timers
):
    engine.ensure_bg_vcam(camera)
    rounds = _drain(captured_timers)
    assert rounds < 100, "ensure_bg_vcam rescheduled itself forever"
    assert not captured_timers, "a retry is still pending after giving up"


def test_background_vcam_stops_quickly(
    engine, camera, never_allocates, captured_timers
):
    engine.ensure_bg_vcam(camera)
    assert _drain(captured_timers) <= 6, "too many attempts before giving up"


# -- the appsink path ------------------------------------------------------


def test_appsink_vcam_retries_are_bounded(
    engine, camera, never_allocates, captured_timers
):
    engine._current_camera = camera
    engine._ensure_vcam_with_retry(camera.id, camera.name)
    rounds = _drain(captured_timers)
    assert rounds < 100, "_ensure_vcam_with_retry rescheduled itself forever"
    assert not captured_timers


def test_retry_stops_when_the_camera_changes(
    engine, camera, never_allocates, captured_timers
):
    engine._current_camera = camera
    engine._ensure_vcam_with_retry(camera.id, camera.name)
    engine._current_camera = None          # user switched away
    _drain(captured_timers)
    assert not captured_timers


# -- the happy path still works -------------------------------------------

def test_no_retry_once_a_device_is_obtained(engine, camera, monkeypatch,
                                            captured_timers):
    monkeypatch.setattr(VirtualCamera, "is_enabled", classmethod(lambda cls: True))
    monkeypatch.setattr(
        VirtualCamera, "ensure_ready",
        classmethod(lambda cls, card_label=None, camera_id="": "/dev/video20"),
    )
    monkeypatch.setattr(
        VirtualCamera, "get_device_for_camera", classmethod(lambda cls, cid: "")
    )
    monkeypatch.setattr(engine, "_start_vcam", lambda dev: None)

    engine._current_camera = camera
    engine._ensure_vcam_with_retry(camera.id, camera.name)
    assert not captured_timers, "retried despite succeeding"
