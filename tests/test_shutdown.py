"""Shutdown must always terminate, bounded, and leave nothing running.

Closing the app must never hang the desktop.  Every component's stop path is
exercised here with a hard wall-clock budget, so a regression that introduces
a join-without-timeout or a blocking wait fails the suite instead of freezing
the user's session.
"""

from __future__ import annotations

import threading
import time
import types

import pytest

from core.camera_manager import CameraManager

# Any single stop() must finish well inside this budget.
STOP_BUDGET_S = 6.0


class _Budget:
    """Context manager asserting the block finishes within *seconds*."""

    def __init__(self, seconds: float, what: str) -> None:
        self.seconds = seconds
        self.what = what

    def __enter__(self):
        self.t0 = time.monotonic()
        return self

    def __exit__(self, *exc):
        elapsed = time.monotonic() - self.t0
        assert elapsed < self.seconds, (
            f"{self.what} took {elapsed:.1f}s (budget {self.seconds}s) — this "
            f"freezes the UI on close"
        )
        return False


def _live_bigcam_threads() -> list[threading.Thread]:
    return [
        t for t in threading.enumerate()
        if t.is_alive() and (
            t.name.startswith("bigcam-")
            or t.name.startswith("bgvcam-")
            or t.name in ("phone-cam", "rec-finalize", "phone-audio-drain")
        )
    ]


# -- CameraManager ---------------------------------------------------------


def test_hotplug_stops_within_budget(monkeypatch):
    monkeypatch.setattr(CameraManager, "_register_backends", lambda self: None)
    mgr = CameraManager()
    monkeypatch.setattr(mgr, "_snapshot_device_state", lambda: None)

    mgr.start_hotplug(interval_ms=100)
    time.sleep(0.2)
    with _Budget(STOP_BUDGET_S, "CameraManager.stop_hotplug"):
        mgr.stop_hotplug()
    assert not _live_bigcam_threads(), (
        f"threads survived shutdown: {[t.name for t in _live_bigcam_threads()]}"
    )


def test_stop_hotplug_while_detecting(monkeypatch):
    """Shutting down mid-detection must not deadlock on the poll lock."""
    monkeypatch.setattr(CameraManager, "_register_backends", lambda self: None)
    mgr = CameraManager()
    monkeypatch.setattr(mgr, "_snapshot_device_state", lambda: None)
    mgr._backends = []
    mgr.start_hotplug(interval_ms=50)
    mgr.detect_cameras_async()
    with _Budget(STOP_BUDGET_S, "stop_hotplug during detection"):
        mgr.stop_hotplug()


# -- StreamEngine ----------------------------------------------------------


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(CameraManager, "_register_backends", lambda self: None)
    from core.stream_engine import StreamEngine

    return StreamEngine(CameraManager())


def test_stop_on_a_never_started_engine(engine):
    with _Budget(STOP_BUDGET_S, "StreamEngine.stop (idle)"):
        engine.stop()


def test_stop_is_idempotent(engine):
    with _Budget(STOP_BUDGET_S, "StreamEngine.stop x3"):
        engine.stop()
        engine.stop()
        engine.stop()


def test_stop_all_bg_vcams_when_empty(engine):
    with _Budget(STOP_BUDGET_S, "stop_all_bg_vcams"):
        engine.stop_all_bg_vcams()
    assert engine.has_active_bg_vcams() is False


def test_bg_vcam_feeder_stop_terminates(monkeypatch):
    """The OpenCV feeder thread must honour its stop event promptly."""
    from core.stream_engine import _BgVcamFeeder

    feeder = _BgVcamFeeder("/dev/video99", "/dev/video98", "Ghost")
    # Never opens: the retry loop must still exit when asked to stop.
    started = feeder.start()
    if not started:
        pytest.skip("OpenCV not available")
    time.sleep(0.15)
    with _Budget(STOP_BUDGET_S, "_BgVcamFeeder.stop"):
        feeder.stop()
    assert feeder._thread is None


# -- VideoRecorder ---------------------------------------------------------


def test_recorder_stop_and_finalize_are_bounded():
    from core.video_recorder import VideoRecorder

    rec = VideoRecorder(camera_manager=None)
    with _Budget(STOP_BUDGET_S, "VideoRecorder.stop + wait_finalize"):
        rec.stop()
        rec.wait_finalize(timeout=2.0)


# -- AudioMonitor ----------------------------------------------------------


def test_audio_stop_all_is_bounded(monkeypatch):
    from core import audio_monitor as am
    from core.audio_monitor import AudioMonitor

    monkeypatch.setattr(
        am.SecureCommandRunner, "run_safe",
        staticmethod(lambda *a, **kw: types.SimpleNamespace(
            returncode=0, stdout="", stderr="")),
    )
    mon = AudioMonitor()
    for name in ("a", "b", "c"):
        mon._pipelines[name] = types.SimpleNamespace(set_state=lambda s: None)
    with _Budget(STOP_BUDGET_S, "AudioMonitor.stop_all"):
        mon.stop_all()
    assert mon._pipelines == {}


# -- PhoneCameraServer -----------------------------------------------------


def test_phone_server_stop_without_start_is_instant():
    from core.phone_camera import PhoneCameraServer

    srv = PhoneCameraServer()
    with _Budget(2.0, "PhoneCameraServer.stop (never started)"):
        srv.stop()


@pytest.mark.slow
def test_phone_server_start_stop_roundtrip():
    from core.phone_camera import PhoneCameraServer

    srv = PhoneCameraServer()
    if not srv.available():
        pytest.skip("aiohttp not installed")
    ok, msg = srv.start(port=18443)
    if not ok:
        pytest.skip(f"server could not bind: {msg}")
    try:
        assert srv.running
    finally:
        with _Budget(10.0, "PhoneCameraServer.stop"):
            srv.stop()
    assert srv.running is False
    time.sleep(0.2)
    leftovers = [t for t in threading.enumerate()
                 if t.is_alive() and t.name == "phone-cam"]
    assert not leftovers, "asyncio thread outlived stop()"


# -- scrcpy / airplay ------------------------------------------------------


def test_scrcpy_stop_without_start(monkeypatch):
    from core.scrcpy_camera import ScrcpyCamera

    cam = ScrcpyCamera()
    with _Budget(2.0, "ScrcpyCamera.stop"):
        cam.stop()


def test_airplay_stop_without_start():
    from core.airplay_receiver import AirPlayReceiver

    rx = AirPlayReceiver()
    with _Budget(2.0, "AirPlayReceiver.stop"):
        rx.stop()


# -- global: nothing left behind ------------------------------------------


def test_no_bigcam_threads_survive_the_module(monkeypatch):
    monkeypatch.setattr(CameraManager, "_register_backends", lambda self: None)
    from core.stream_engine import StreamEngine

    mgr = CameraManager()
    monkeypatch.setattr(mgr, "_snapshot_device_state", lambda: None)
    eng = StreamEngine(mgr)

    mgr.start_hotplug(interval_ms=80)
    time.sleep(0.2)

    with _Budget(STOP_BUDGET_S * 2, "full shutdown sequence"):
        eng.stop()
        eng.stop_all_bg_vcams()
        mgr.stop_hotplug()

    time.sleep(0.3)
    survivors = _live_bigcam_threads()
    assert not survivors, f"leaked threads: {[t.name for t in survivors]}"
