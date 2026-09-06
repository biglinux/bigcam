"""The window close path: bounded, ordered, and it flushes the recording.

These build the real BigDigicamWindow, so they need a display.
"""

from __future__ import annotations

import threading
import time

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display available", allow_module_level=True)

pytestmark = pytest.mark.gtk

CLOSE_BUDGET_S = 30.0


@pytest.fixture
def window(monkeypatch, settings):
    """A real window with all hardware access stubbed out."""
    from core.camera_manager import CameraManager
    from core.virtual_camera import VirtualCamera
    from ui import window as window_mod

    monkeypatch.setattr(CameraManager, "_register_backends", lambda self: None)
    monkeypatch.setattr(CameraManager, "detect_cameras_async",
                        lambda self, force_emit=False: None)
    monkeypatch.setattr(CameraManager, "start_hotplug",
                        lambda self, interval_ms=5000: None)
    monkeypatch.setattr(window_mod.SettingsManager, "__new__",
                        lambda cls, *a, **kw: settings)
    monkeypatch.setattr(window_mod.SettingsManager, "__init__",
                        lambda self, *a, **kw: None)
    monkeypatch.setattr(VirtualCamera, "cleanup_dynamic_devices",
                        classmethod(lambda cls: None))
    monkeypatch.setattr("core.audio_monitor.AudioMonitor.detect_all",
                        lambda self: None)

    app = Adw.Application(application_id="br.com.biglinux.bigcam.test")
    win = window_mod.BigDigicamWindow(app)
    yield win
    try:
        win.destroy()
    except Exception:
        pass


def test_close_with_nothing_running_is_fast(window):
    t0 = time.monotonic()
    window._cleanup_and_close(quitting=True)
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0, f"idle close took {elapsed:.1f}s"


def test_close_waits_for_the_recording_to_finalize(window, monkeypatch):
    """An in-progress recording must be flushed before the process exits."""
    finalized = threading.Event()

    def _slow_finalize(timeout=20.0):
        time.sleep(0.4)
        finalized.set()

    window._video_recorder._recording = True
    monkeypatch.setattr(window._video_recorder, "stop", lambda: "/tmp/x.mkv")
    monkeypatch.setattr(window._video_recorder, "wait_finalize", _slow_finalize)

    window._cleanup_and_close(quitting=True)
    assert finalized.is_set(), (
        "close returned before the muxer finished — the file is truncated"
    )


def test_close_does_not_wait_when_not_recording(window, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(
        window._video_recorder, "wait_finalize",
        lambda timeout=20.0: called.__setitem__("n", called["n"] + 1),
    )
    window._video_recorder._recording = False
    window._cleanup_and_close(quitting=True)
    assert called["n"] == 0


def test_a_wedged_cleanup_step_cannot_block_the_close(window, monkeypatch):
    """A hung modprobe must not leave the user with an unclosable window."""
    from core.virtual_camera import VirtualCamera
    from ui import window as window_mod

    monkeypatch.setattr(window_mod, "_CLEANUP_TIMEOUT_S", 1.0)
    monkeypatch.setattr(
        VirtualCamera, "cleanup_dynamic_devices",
        classmethod(lambda cls: time.sleep(30)),
    )

    t0 = time.monotonic()
    window._cleanup_and_close(quitting=True)
    elapsed = time.monotonic() - t0
    assert elapsed < 6.0, (
        f"close blocked {elapsed:.1f}s on a wedged cleanup step (budget 1s)"
    )


def test_full_close_sequence_is_bounded(window):
    t0 = time.monotonic()
    handled = window._on_close(window)
    if handled:
        # A dialog was shown (something is active) — take the "stop" branch.
        window._on_close_response(None, "stop")
    elapsed = time.monotonic() - t0
    assert elapsed < CLOSE_BUDGET_S, f"close sequence took {elapsed:.1f}s"


def test_cleanup_is_idempotent(window):
    window._cleanup_and_close(quitting=True)
    window._cleanup_and_close(quitting=True)


def test_no_threads_leak_after_close(window):
    window._cleanup_and_close(quitting=True)
    time.sleep(0.4)
    survivors = [
        t.name for t in threading.enumerate()
        if t.is_alive() and (
            t.name.startswith("bigcam-hotplug")
            or t.name.startswith("bgvcam-")
            or t.name == "phone-cam"
        )
    ]
    assert not survivors, f"leaked threads after close: {survivors}"
