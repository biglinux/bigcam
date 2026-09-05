"""VirtualCamera: allocation bookkeeping and non-destructive cleanup.

The critical invariant: BigCam must never delete a v4l2loopback device it
did not create.  Other apps (OBS, Droidcam, ...) own devices too.
"""

from __future__ import annotations

import types

import pytest

from core import virtual_camera as vc
from core.virtual_camera import VirtualCamera

LIST_DEVICES = """\
BigCam Virtual 1 (platform:v4l2loopback-000):
\t/dev/video20

BigCam Virtual 2 (platform:v4l2loopback-001):
\t/dev/video21

OBS Virtual Camera (platform:v4l2loopback-002):
\t/dev/video30

Integrated Camera: Integrated C (usb-0000:00:14.0-8):
\t/dev/video0
"""


@pytest.fixture(autouse=True)
def _reset_class_state():
    VirtualCamera._allocations = {}
    VirtualCamera._dynamic_devices = set()
    VirtualCamera._enabled = False
    VirtualCamera._max_devices = 5
    VirtualCamera._name_template = "BigCam Virtual"
    VirtualCamera._labels_synced = False
    VirtualCamera._load_attempted = False
    VirtualCamera._dynamic_supported = None
    yield
    VirtualCamera._allocations = {}
    VirtualCamera._dynamic_devices = set()


@pytest.fixture
def fake_v4l2(monkeypatch):
    """Stub out v4l2-ctl --list-devices and record every privileged call."""
    calls: list[list[str]] = []

    def _sub_run(args, *a, **kw):
        calls.append(list(args))
        if args[:2] == ["v4l2-ctl", "--list-devices"]:
            return types.SimpleNamespace(
                returncode=0, stdout=LIST_DEVICES, stderr=""
            )
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def _run_safe(args, *a, **kw):
        calls.append(list(args))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vc.subprocess, "run", _sub_run)
    monkeypatch.setattr(vc.SecureCommandRunner, "run_safe", staticmethod(_run_safe))
    return calls


# -- discovery -------------------------------------------------------------


def test_find_all_loopback_devices(fake_v4l2):
    devs = VirtualCamera.find_all_loopback_devices()
    assert devs == ["/dev/video20", "/dev/video21", "/dev/video30"]
    assert "/dev/video0" not in devs


def test_device_labels_are_parsed(fake_v4l2):
    labels = VirtualCamera._get_device_labels()
    assert labels["/dev/video20"] == "BigCam Virtual 1"
    assert labels["/dev/video30"] == "OBS Virtual Camera"


# -- the destructive-cleanup bug ------------------------------------------


def test_cleanup_never_deletes_foreign_devices(fake_v4l2, monkeypatch):
    """Only devices matching BigCam's own naming template may be removed."""
    deleted: list[str] = []
    monkeypatch.setattr(
        VirtualCamera, "_delete_dynamic_device",
        classmethod(lambda cls, dev: deleted.append(dev) or True),
    )
    monkeypatch.setattr(VirtualCamera, "_is_dynamic_supported",
                        classmethod(lambda cls: True))

    VirtualCamera.cleanup_dynamic_devices()

    assert "/dev/video30" not in deleted, (
        "BigCam deleted OBS's virtual camera device"
    )
    assert set(deleted) <= {"/dev/video20", "/dev/video21"}


def test_cleanup_removes_own_stale_devices(fake_v4l2, monkeypatch):
    deleted: list[str] = []
    monkeypatch.setattr(
        VirtualCamera, "_delete_dynamic_device",
        classmethod(lambda cls, dev: deleted.append(dev) or True),
    )
    monkeypatch.setattr(VirtualCamera, "_is_dynamic_supported",
                        classmethod(lambda cls: True))
    VirtualCamera.cleanup_dynamic_devices()
    assert set(deleted) == {"/dev/video20", "/dev/video21"}


# -- allocation ------------------------------------------------------------


def test_allocate_is_idempotent_per_camera(fake_v4l2, monkeypatch):
    monkeypatch.setattr(VirtualCamera, "_is_dynamic_supported",
                        classmethod(lambda cls: True))
    dev1 = VirtualCamera.allocate_device("cam-a")
    dev2 = VirtualCamera.allocate_device("cam-a")
    assert dev1 == dev2 == "/dev/video20"


def test_allocate_respects_max_devices(fake_v4l2, monkeypatch):
    monkeypatch.setattr(VirtualCamera, "_is_dynamic_supported",
                        classmethod(lambda cls: True))
    monkeypatch.setattr(VirtualCamera, "_add_dynamic_device",
                        classmethod(lambda cls, label: ""))
    VirtualCamera.set_max_devices(2)
    assert VirtualCamera.allocate_device("a")
    assert VirtualCamera.allocate_device("b")
    assert VirtualCamera.allocate_device("c") == ""


def test_release_frees_the_dynamic_device(fake_v4l2, monkeypatch):
    """Releasing must actually destroy devices BigCam created dynamically."""
    deleted: list[str] = []
    monkeypatch.setattr(
        VirtualCamera, "_delete_dynamic_device",
        classmethod(lambda cls, dev: deleted.append(dev) or True),
    )
    VirtualCamera._allocations["cam-a"] = "/dev/video40"
    VirtualCamera._dynamic_devices.add("/dev/video40")

    VirtualCamera.release_device("cam-a")

    assert VirtualCamera.get_device_for_camera("cam-a") == ""
    assert deleted == ["/dev/video40"], (
        "dynamic devices leak until app exit if release does not delete them"
    )


def test_release_keeps_devices_it_did_not_create(fake_v4l2, monkeypatch):
    deleted: list[str] = []
    monkeypatch.setattr(
        VirtualCamera, "_delete_dynamic_device",
        classmethod(lambda cls, dev: deleted.append(dev) or True),
    )
    VirtualCamera._allocations["cam-a"] = "/dev/video20"  # pre-existing, static
    VirtualCamera.release_device("cam-a")
    assert deleted == []


def test_ensure_ready_returns_empty_when_disabled(fake_v4l2):
    VirtualCamera.set_enabled(False)
    assert VirtualCamera.ensure_ready(camera_id="x") == ""


# -- privileged command shape ---------------------------------------------


def test_privileged_calls_go_through_the_validating_helper(monkeypatch):
    """Nothing may invoke modprobe or v4l2loopback-ctl directly any more."""
    seen: list[list[str]] = []

    def _run(args, *a, **kw):
        seen.append(list(args))
        return types.SimpleNamespace(returncode=0, stdout="/dev/video20", stderr="")

    monkeypatch.setattr(vc.SecureCommandRunner, "run_safe", staticmethod(_run))
    vc._run_privileged("load")

    assert seen, "no privileged command was issued"
    cmd = seen[0]
    assert cmd[:2] == ["sudo", "-n"]
    assert cmd[2].endswith("bigcam-v4l2loopback")
    assert "modprobe" not in cmd
    assert cmd[3:] == ["load"], "extra arguments leaked into the privileged call"


def test_privileged_falls_back_to_pkexec(monkeypatch):
    """Users outside the wheel group must still be able to authenticate."""
    seen: list[list[str]] = []

    def _run(args, *a, **kw):
        seen.append(list(args))
        rc = 1 if args[0] == "sudo" else 0
        return types.SimpleNamespace(returncode=rc, stdout="", stderr="denied")

    monkeypatch.setattr(vc.shutil, "which", lambda n: "/usr/bin/pkexec")
    monkeypatch.setattr(vc.SecureCommandRunner, "run_safe", staticmethod(_run))
    result = vc._run_privileged("unload")

    assert result.returncode == 0
    assert len(seen) == 2
    assert seen[1][0] == "/usr/bin/pkexec"
    assert seen[1][1].endswith("bigcam-v4l2loopback")


def test_privileged_never_raises(monkeypatch):
    def _boom(args, *a, **kw):
        raise OSError("no sudo here")

    monkeypatch.setattr(vc.shutil, "which", lambda n: None)
    monkeypatch.setattr(vc.SecureCommandRunner, "run_safe", staticmethod(_boom))
    result = vc._run_privileged("load")
    assert result.returncode != 0


def test_dynamic_devices_stay_inside_the_pool(fake_v4l2, monkeypatch):
    """The helper rejects out-of-range devices; never even ask for one."""
    requested: list[str] = []

    def _run(args, *a, **kw):
        requested.append(list(args))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(vc.SecureCommandRunner, "run_safe", staticmethod(_run))
    monkeypatch.setattr(vc.os.path, "exists", lambda p: False)

    VirtualCamera._add_dynamic_device("BigCam Virtual 1")

    adds = [c for c in requested if "add" in c]
    assert adds, "no add command issued"
    device = adds[0][-1]
    num = int(device.rsplit("video", 1)[1])
    assert vc.DEVICE_BASE <= num < vc.DEVICE_BASE + vc.DEVICE_POOL_SIZE


def test_pool_exhaustion_is_reported_not_ignored(fake_v4l2, monkeypatch):
    monkeypatch.setattr(vc.os.path, "exists", lambda p: True)  # every slot taken
    assert VirtualCamera._add_dynamic_device("BigCam Virtual 1") == ""
