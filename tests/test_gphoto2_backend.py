"""gPhoto2 backend: parsing, and "leave the system as you found it"."""

from __future__ import annotations

import types

import pytest

from constants import BackendType, ControlType
from core.backends import gphoto2_backend as gp
from core.backends.gphoto2_backend import GPhoto2Backend

AUTO_DETECT = """\
Model                          Port
----------------------------------------------------------
Canon EOS 250D                 usb:001,012
Nikon DSC D5600                usb:001,015
"""

CONFIG_BLOCK = """\
Label: ISO Speed
Readonly: 0
Type: RADIO
Current: 400
Choice: 0 100
Choice: 1 200
Choice: 2 400
END
"""


@pytest.fixture
def backend():
    b = GPhoto2Backend()
    GPhoto2Backend._gvfs_stopped = False
    GPhoto2Backend._active_streams = {}
    return b


@pytest.fixture
def runner(monkeypatch):
    """Record every privileged/system command instead of running it."""
    calls: list[list[str]] = []

    def _run(args, *a, **kw):
        calls.append(list(args))
        stdout = ""
        if args[:2] == ["gphoto2", "--auto-detect"]:
            stdout = AUTO_DETECT
        return types.SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(gp.SecureCommandRunner, "run_safe", staticmethod(_run))
    return calls


# -- detection / parsing ---------------------------------------------------


def test_detect_parses_ports(backend, runner, monkeypatch):
    monkeypatch.setattr(gp.time, "sleep", lambda s: None)
    cams = backend.detect_cameras()
    assert [c.name for c in cams] == ["Canon EOS 250D", "Nikon DSC D5600"]
    assert cams[0].extra["port"] == "usb:001,012"
    assert cams[0].backend is BackendType.GPHOTO2


def test_detect_gives_each_camera_its_own_udp_port(backend, runner, monkeypatch):
    monkeypatch.setattr(gp.time, "sleep", lambda s: None)
    cams = backend.detect_cameras()
    ports = [c.extra["udp_port"] for c in cams]
    assert len(set(ports)) == len(ports), "UDP port collision between cameras"


def test_parse_config_radio(backend):
    ctrl = GPhoto2Backend._parse_config("/main/imgsettings/iso", CONFIG_BLOCK)
    assert ctrl.control_type is ControlType.MENU
    assert ctrl.value == "400"
    assert ctrl.choices == ["100", "200", "400"]


def test_parse_config_marks_readonly(backend):
    block = CONFIG_BLOCK.replace("Readonly: 0", "Readonly: 1")
    ctrl = GPhoto2Backend._parse_config("/main/status/batterylevel", block)
    assert ctrl.flags == "read-only"


def test_categorize_uses_leaf_then_section(backend):
    from constants import ControlCategory

    assert GPhoto2Backend._categorize("/main/imgsettings/iso") is ControlCategory.EXPOSURE
    assert (
        GPhoto2Backend._categorize("/main/capturesettings/unknownthing")
        is ControlCategory.CAPTURE
    )
    assert GPhoto2Backend._categorize("/main/weird/thing") is ControlCategory.ADVANCED


# -- do not permanently modify the user's system --------------------------


def test_kill_gvfs_never_masks_the_unit(backend, runner):
    """Masking survives BigCam and breaks camera mounting desktop-wide."""
    backend._kill_gvfs()
    for call in runner:
        assert "mask" not in call, (
            f"BigCam permanently masked a systemd unit: {' '.join(call)}"
        )


def test_kill_gvfs_only_stops_the_unit(backend, runner):
    backend._kill_gvfs()
    systemctl = [c for c in runner if c and c[0] == "systemctl"]
    assert systemctl, "expected the GVFS monitor to be stopped"
    assert all(c[2] == "stop" for c in systemctl)


def test_restore_gvfs_restarts_what_was_stopped(backend, runner):
    backend._kill_gvfs()
    runner.clear()
    backend.restore_gvfs()
    verbs = [c[2] for c in runner if c and c[0] == "systemctl"]
    assert "start" in verbs, "GVFS was never handed back to the desktop"


def test_restore_gvfs_unmasks_legacy_leftovers(backend, runner):
    """Older builds masked the unit; restore must repair that too."""
    backend.restore_gvfs()
    verbs = [c[2] for c in runner if c and c[0] == "systemctl"]
    assert "unmask" in verbs


def test_restore_is_safe_without_a_prior_stop(backend, runner):
    backend.restore_gvfs()  # must not raise


# -- temp files ------------------------------------------------------------


def test_debug_log_is_not_in_world_writable_tmp(backend, runner, monkeypatch):
    """A fixed /tmp path invites symlink attacks and multi-user collisions."""
    captured: list[str] = []

    def _run(args, *a, **kw):
        captured.extend(args)
        return types.SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(gp.SecureCommandRunner, "run_safe", staticmethod(_run))
    monkeypatch.setattr(gp.time, "sleep", lambda s: None)

    from core.camera_backend import CameraInfo

    cam = CameraInfo(id="g", name="Cam", backend=BackendType.GPHOTO2,
                     device_path="usb:001,012", extra={"port": "usb:001,012"})
    backend.capture_photo(cam, "/tmp/out.jpg")

    logfiles = [
        a for a in captured
        if isinstance(a, str) and a.endswith("gphoto2_capture_debug.log")
    ]
    assert logfiles, "debug logfile argument not found"

    from utils import xdg

    cache = xdg.cache_dir()
    for path in logfiles:
        assert path.startswith(cache), (
            f"debug log escapes the private cache dir: {path}"
        )


def test_debug_log_path_is_not_hardcoded():
    """Guard against regressing to a fixed, world-writable /tmp path."""
    import inspect

    src = inspect.getsource(GPhoto2Backend.capture_photo)
    assert '"/tmp/' not in src and "'/tmp/" not in src, (
        "capture_photo hardcodes a /tmp path again"
    )
