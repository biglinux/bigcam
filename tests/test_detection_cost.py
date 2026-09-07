"""Detection must not pay for retries and processes it cannot use.

Two costs on the hot path.

detect_cameras retried three times and slept 0.5s after *every* attempt,
including the last one and including the case where nothing could possibly
change.  A machine with no camera attached paid 1.5s of sleeping to be told
three times what the first call already said, and up to 16.5s if v4l2-ctl
hung on each attempt.  The retry is there for the moment after a hotplug
when a node exists but the driver will not answer queries on it yet — so it
is worth waiting only when a node exists.

The hotplug poll spawned lsusb every few seconds for the entire life of the
process, to notice a camera being plugged in.  Reading the same identifiers
out of sysfs costs 0.14ms against 5.3ms and no process at all.
"""

from __future__ import annotations

import subprocess

import pytest

from core.backends.v4l2_backend import V4L2Backend
from core.camera_manager import _usb_signature, _usb_signature_via_lsusb

TWO_CAMERAS = """\
ASUS FHD webcam: ASUS FHD webca (usb-0000:00:14.0-7):
\t/dev/video0
\t/dev/video1

bcm2835-isp (platform:bcm2835-isp):
\t/dev/video2
"""


@pytest.fixture
def backend(monkeypatch):
    """_parse_devices queries each node for Video Capture support.

    That is a real ioctl against a real device, so the two nodes in the
    canned output above have to be answered for.  video0 is the capture
    node; video1 is the metadata node a UVC webcam also exposes.
    """
    bk = V4L2Backend()
    monkeypatch.setattr(
        V4L2Backend, "_is_capture_device",
        lambda self, dev: dev == "/dev/video0",
    )
    monkeypatch.setattr(V4L2Backend, "_get_formats", lambda self, dev: [])
    return bk


@pytest.fixture
def calls(monkeypatch):
    """Record v4l2-ctl invocations and the sleeps between them."""
    from core.backends import v4l2_backend

    record = {"runs": 0, "slept": 0.0}

    def _sleep(seconds):
        record["slept"] += seconds

    monkeypatch.setattr(v4l2_backend.time, "sleep", _sleep)
    return record


def _stub_run(monkeypatch, record, stdout="", returncode=0, raises=None):
    from core.backends import v4l2_backend

    def _run(*_a, **_k):
        record["runs"] += 1
        if raises:
            raise raises
        return subprocess.CompletedProcess([], returncode, stdout, "")

    monkeypatch.setattr(v4l2_backend.subprocess, "run", _run)


def _stub_nodes(monkeypatch, nodes):
    from core.backends import v4l2_backend

    monkeypatch.setattr(v4l2_backend.glob, "glob", lambda _p: list(nodes))


# -- the happy path costs one call -----------------------------------------


def test_a_camera_is_found_on_the_first_attempt(backend, calls, monkeypatch):
    _stub_run(monkeypatch, calls, stdout=TWO_CAMERAS)
    _stub_nodes(monkeypatch, ["/dev/video0"])
    assert backend.detect_cameras()
    assert calls["runs"] == 1
    assert calls["slept"] == 0.0, "slept despite succeeding immediately"


# -- no nodes means nothing to wait for ------------------------------------


def test_no_video_nodes_does_not_retry(backend, calls, monkeypatch):
    _stub_run(monkeypatch, calls, stdout="")
    _stub_nodes(monkeypatch, [])
    assert backend.detect_cameras() == []
    assert calls["runs"] == 1, (
        f"asked {calls['runs']} times with no device node present"
    )
    assert calls["slept"] == 0.0


def test_no_video_nodes_after_a_failure_does_not_retry(backend, calls, monkeypatch):
    _stub_run(monkeypatch, calls, returncode=1)
    _stub_nodes(monkeypatch, [])
    assert backend.detect_cameras() == []
    assert calls["runs"] == 1


# -- a node that is not answering yet is worth waiting for -----------------


def test_a_present_but_silent_node_is_retried(backend, calls, monkeypatch):
    _stub_run(monkeypatch, calls, stdout="")
    _stub_nodes(monkeypatch, ["/dev/video0"])
    assert backend.detect_cameras() == []
    assert calls["runs"] == 3, "gave up before the driver could answer"


def test_the_last_attempt_is_not_followed_by_a_sleep(backend, calls, monkeypatch):
    _stub_run(monkeypatch, calls, stdout="")
    _stub_nodes(monkeypatch, ["/dev/video0"])
    backend.detect_cameras()
    expected = (backend._DETECT_ATTEMPTS - 1) * backend._DETECT_BACKOFF_S
    assert calls["slept"] == pytest.approx(expected), (
        f"slept {calls['slept']}s for {calls['runs']} attempts; the wait "
        f"after the final attempt buys nothing"
    )


def test_an_exception_is_retried_but_bounded(backend, calls, monkeypatch):
    _stub_run(monkeypatch, calls, raises=OSError("v4l2-ctl missing"))
    _stub_nodes(monkeypatch, ["/dev/video0"])
    assert backend.detect_cameras() == []
    assert calls["runs"] == backend._DETECT_ATTEMPTS


def test_a_camera_appearing_on_the_second_attempt_is_returned(
    backend, calls, monkeypatch
):
    from core.backends import v4l2_backend

    outputs = ["", TWO_CAMERAS]

    def _run(*_a, **_k):
        calls["runs"] += 1
        return subprocess.CompletedProcess([], 0, outputs.pop(0), "")

    monkeypatch.setattr(v4l2_backend.subprocess, "run", _run)
    _stub_nodes(monkeypatch, ["/dev/video0"])
    assert backend.detect_cameras()
    assert calls["runs"] == 2


# -- the USB signature -----------------------------------------------------


def test_the_signature_is_non_empty_on_this_machine():
    assert _usb_signature(), "no USB devices found at all; sysfs read failed"


def test_the_signature_is_stable_across_calls():
    assert _usb_signature() == _usb_signature()


def test_the_signature_names_devices_not_interfaces():
    """Root hubs and interface nodes carry no idVendor; they must be skipped."""
    for entry in _usb_signature().split(","):
        name, vendor, product = entry.split(":")
        assert len(vendor) == 4 and len(product) == 4, entry
        assert ":" not in name


def test_an_unreadable_sysfs_falls_back_to_lsusb(monkeypatch):
    import core.camera_manager as mod

    monkeypatch.setattr(
        mod.os, "listdir", lambda _p: (_ for _ in ()).throw(OSError("no sysfs"))
    )
    called: list[bool] = []
    monkeypatch.setattr(
        mod, "_usb_signature_via_lsusb", lambda: called.append(True) or "lsusb-out"
    )
    assert _usb_signature() == "lsusb-out"
    assert called == [True]


def test_the_lsusb_fallback_returns_empty_on_failure(monkeypatch):
    import core.camera_manager as mod

    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no lsusb")),
    )
    assert _usb_signature_via_lsusb() == ""


def test_a_nonzero_lsusb_is_not_treated_as_a_device_list(monkeypatch):
    import core.camera_manager as mod

    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, "partial output", ""),
    )
    assert _usb_signature_via_lsusb() == ""


def test_no_process_is_spawned_for_the_signature(monkeypatch):
    """The whole point: this runs every few seconds, forever."""
    import core.camera_manager as mod

    def _boom(*_a, **_k):
        raise AssertionError("spawned a process to read the USB device list")

    monkeypatch.setattr(mod.subprocess, "run", _boom)
    assert _usb_signature()
