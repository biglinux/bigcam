"""Hardware access from the controls page must not run on the main thread.

Reading or writing a camera control means a subprocess.  For V4L2 that is one
quick v4l2-ctl call; for a gphoto2 camera, get_controls retries
--list-all-config three times with a 15s timeout and sleeps between attempts,
then makes one more attempt on a re-detected port.  Nothing in the interface
redraws while that happens.

Two places did it on the main thread:

  _reload_controls  — scheduled with GLib.idle_add, so the read ran on the
                      main loop.  Touching one setting on a DSLR could sit
                      the whole window frozen for the better part of a minute.
  _on_reset         — a clicked handler calling reset_all_controls, which is
                      one subprocess per control, in a row.

The rest of the page already knew this: _apply spawns a thread with the
comment "to avoid blocking UI".  These two were missed.
"""

from __future__ import annotations

import threading

import pytest

from constants import BackendType, ControlCategory, ControlType
from core.camera_backend import CameraControl, CameraInfo
from ui.camera_controls_page import CameraControlsPage

MAIN_THREAD = threading.current_thread()


def _ctrl(cid="brightness", value=10, default=0):
    return CameraControl(
        id=cid, name=cid, category=ControlCategory.IMAGE,
        control_type=ControlType.INTEGER, value=value, default=default,
        minimum=-64, maximum=64,
    )


class _Manager:
    """Records which thread each hardware call was made from."""

    def __init__(self, controls=None):
        self._controls = controls if controls is not None else [_ctrl()]
        self.threads: list[threading.Thread] = []
        self.done = threading.Event()

    def get_controls(self, _camera):
        self.threads.append(threading.current_thread())
        self.done.set()
        return self._controls

    def reset_all_controls(self, _camera, _ctrls):
        self.threads.append(threading.current_thread())
        self.done.set()

    def apply_anti_flicker(self, _camera):
        pass


@pytest.fixture
def page():
    """A bare page: __init__ builds widgets we do not need here."""
    obj = CameraControlsPage.__new__(CameraControlsPage)
    obj._camera = CameraInfo(
        id="gphoto2:usb:001,005", name="DSLR",
        backend=BackendType.GPHOTO2, device_path="",
    )
    obj._controls = []
    obj._ctrl_widgets = {}
    obj._ctrl_rows = {}
    obj._resetting = False
    obj._engine = None
    obj._debounce_sources = {}
    # Assigned per instance in __init__ despite the constant-style name.
    obj._DEPENDENCIES = {}
    return obj


# -- reading the hardware back --------------------------------------------


def test_reload_does_not_read_on_the_calling_thread(page):
    manager = _Manager()
    page._manager = manager

    page._reload_controls()
    assert manager.done.wait(timeout=5), "the read never happened"
    assert manager.threads[0] is not MAIN_THREAD, (
        "get_controls ran on the main thread; on a gphoto2 camera that "
        "freezes the window for up to a minute"
    )


def test_reload_returns_immediately(page):
    """It is an idle callback: it must hand control straight back."""
    import time

    class _Slow(_Manager):
        def get_controls(self, _camera):
            time.sleep(0.5)
            return super().get_controls(_camera)

    page._manager = _Slow()
    started = time.monotonic()
    page._reload_controls()
    assert time.monotonic() - started < 0.2


def test_reload_returns_false_so_the_idle_source_is_removed(page):
    page._manager = _Manager()
    assert page._reload_controls() is False


def test_reload_without_a_camera_is_a_noop(page):
    page._camera = None
    page._manager = _Manager()
    assert page._reload_controls() is False
    assert page._manager.threads == []


# -- applying what was read -----------------------------------------------


def test_values_are_applied_for_the_camera_that_was_read(page):
    controls = [_ctrl(value=42)]
    assert page._apply_control_values("gphoto2:usb:001,005", controls) is False
    assert page._controls == controls


def test_values_from_a_previous_camera_are_discarded(page):
    """The read is slow enough that the user may have switched away."""
    page._controls = ["untouched"]
    page._apply_control_values("v4l2:/dev/video0", [_ctrl()])
    assert page._controls == ["untouched"], "applied another camera's values"


def test_values_are_discarded_if_the_camera_went_away(page):
    page._camera = None
    page._controls = ["untouched"]
    page._apply_control_values("gphoto2:usb:001,005", [_ctrl()])
    assert page._controls == ["untouched"]


def test_the_resetting_guard_is_cleared_afterwards(page):
    page._apply_control_values("gphoto2:usb:001,005", [_ctrl()])
    assert page._resetting is False, (
        "left the guard set, so later user edits would be ignored"
    )


# -- resetting to defaults -------------------------------------------------


def test_reset_does_not_write_on_the_calling_thread(page):
    manager = _Manager()
    page._manager = manager

    page._on_reset(None, [_ctrl()])
    assert manager.done.wait(timeout=5), "the reset never happened"
    assert manager.threads[0] is not MAIN_THREAD, (
        "reset_all_controls ran on the main thread: one subprocess per "
        "control, in a row, from a button handler"
    )


def test_reset_updates_the_widget_without_waiting_for_the_device(page):
    import time

    from gi.repository import Gtk

    class _Slow(_Manager):
        def reset_all_controls(self, _camera, _ctrls):
            time.sleep(0.5)
            return super().reset_all_controls(_camera, _ctrls)

    page._manager = _Slow()
    adjustment = Gtk.Adjustment(lower=-64, upper=64, value=55)
    page._ctrl_widgets = {"brightness": ("int", adjustment)}

    started = time.monotonic()
    page._on_reset(None, [_ctrl(value=55, default=0)])
    elapsed = time.monotonic() - started

    assert elapsed < 0.2, f"the handler blocked for {elapsed:.2f}s"
    assert adjustment.get_value() == 0.0, "the widget was not reset"


def test_reset_clears_the_guard(page):
    page._manager = _Manager()
    page._on_reset(None, [_ctrl()])
    assert page._resetting is False


def test_reset_without_a_camera_is_a_noop(page):
    page._camera = None
    page._manager = _Manager()
    page._on_reset(None, [_ctrl()])
    assert page._manager.threads == []
