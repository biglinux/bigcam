from contextlib import contextmanager
from importlib.machinery import SourceFileLoader
from importlib.util import spec_from_loader, module_from_spec
from pathlib import Path

import pytest
from utils.urls import camera_url, camera_url_id, public_camera_name, gst_quote
from utils.command_runner import SecureCommandRunner
from core.backends.ip_backend import IPBackend

ROOT = Path(__file__).resolve().parents[2]


def load_script(name, path):
    loader = SourceFileLoader(name, str(ROOT / path))
    module = module_from_spec(spec_from_loader(name, loader))
    loader.exec_module(module)
    return module


helper = load_script("bigcam_helper_test", "usr/lib/bigcam/virtual-camera-helper")
migration = load_script("bigcam_migration_test", "usr/share/biglinux/bigcam/script/migrate-legacy-policy.py")


@pytest.mark.parametrize("args", [[], ["load", "-C", "/tmp/evil"], ["unload"],
    ["create", "../session", "name"], ["create", "f"*32, "name\nexec"],
    ["create", "f"*32, "a"*32], ["delete", "f"*32, "/dev/video0"],
    ["delete", "f"*32, "/dev/video20", "-f"], ["delete", "f"*32, "../../dev/video20"]])
def test_privileged_argument_allowlist(args):
    with pytest.raises(ValueError):
        helper.validate(args)


def test_fixed_load_cannot_inherit_modprobe_options(monkeypatch):
    monkeypatch.setenv("MODPROBE_OPTIONS", "-C /tmp/untrusted")
    monkeypatch.setattr(helper.shutil, "which", lambda *a, **kw: "/usr/sbin/modprobe")
    calls = []
    monkeypatch.setattr(helper.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    helper.run("modprobe", "--ignore-install", "--config", "/dev/null", "v4l2loopback", "devices=0")
    assert calls[0][0][0] == ["/usr/sbin/modprobe", "--ignore-install", "--config", "/dev/null", "v4l2loopback", "devices=0"]
    assert calls[0][1]["env"] == {"PATH": helper.SAFE_PATH, "LC_ALL": "C"}
    assert "shell" not in calls[0][1]


@pytest.mark.parametrize("uid,session", [(1001, "a"*32), (1000, "b"*32)])
def test_delete_requires_uid_and_session(monkeypatch, uid, session):
    @contextmanager
    def ledger():
        yield {"/dev/video20": {"uid": 1000, "session": "a"*32, "label": "BigCam"}}
    monkeypatch.setattr(helper, "ledger", ledger)
    monkeypatch.setattr(helper.Path, "exists", lambda self: True)
    monkeypatch.setattr(helper, "run", lambda *a: pytest.fail("Must not invoke ctl"))
    with pytest.raises(PermissionError):
        helper.perform("delete", session, "/dev/video20", uid)


def test_migration_removes_only_known_rules():
    legacy = "%wheel ALL=(root) NOPASSWD: /usr/bin/modprobe v4l2loopback *\n"
    custom = "%admin ALL=(root) /usr/bin/other\n# Administrator configuration\n"
    assert migration.filtered(legacy + custom) == custom


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https:///bad", "https://x:99999/", "https://[", "http://x/\nname"])
def test_invalid_camera_urls(url):
    with pytest.raises(ValueError):
        camera_url(url)


def test_camera_ids_and_names_do_not_disclose_passwords():
    url = "RTSPS://alice:secret@example.invalid:8554/live?token=hidden"
    assert camera_url(url).startswith("rtsps://")
    assert public_camera_name(url) == "rtsps://example.invalid"
    assert camera_url_id(url).startswith("ip:")
    assert "secret" not in camera_url_id(url)
    camera = IPBackend().cameras_from_urls([{"url": url, "name": url}])[0]
    source = IPBackend().get_gst_source(camera)
    assert source.startswith("rtspsrc ")
    assert "secret" not in camera.name


def test_property_quoting_handles_backslashes_and_quotes():
    assert gst_quote('a\\b"c') == '"a\\\\b\\"c"'
    with pytest.raises(ValueError):
        gst_quote("a\n!")


def test_subprocess_rejects_shell_command_strings():
    with pytest.raises(ValueError):
        SecureCommandRunner.run_safe("echo unexpected")


@pytest.mark.parametrize("alive,inode,owner,expected", [
    (False, 41, 1000, True),
    (True, 41, 1000, False),
    (False, 99, 1000, False),
    (False, 41, 1001, False),
])
def test_reclaim_only_dead_owned_device(monkeypatch, alive, inode, owner, expected):
    records = {"/dev/video20": {"uid": owner, "session": "a" * 32, "label": "BigCam",
                                "pid": 123, "started": "old", "inode": 41}}
    @contextmanager
    def ledger():
        yield records
    calls = []
    monkeypatch.setattr(helper, "ledger", ledger)
    monkeypatch.setattr(helper.Path, "exists", lambda p: str(p) == "/dev/video20")
    monkeypatch.setattr(helper, "current_label", lambda device: "BigCam")
    monkeypatch.setattr(helper, "device_identity", lambda device: inode)
    monkeypatch.setattr(helper.os, "getppid", lambda: 456)
    monkeypatch.setattr(helper, "process_identity", lambda pid, uid: "new" if pid == 456 else ("old" if alive else None))
    monkeypatch.setattr(helper, "run", lambda *args: calls.append(args))
    monkeypatch.setattr(helper, "load", lambda: None)
    helper.perform("create", "b" * 32, "BigCam", 1000)
    assert (("v4l2loopback-ctl", "delete", "/dev/video20") in calls) is expected
    assert records["/dev/video21"]["pid"] == 456


def test_helper_process_identity_detects_pid_reuse():
    import os
    assert helper.process_identity(os.getpid(), os.getuid())
    assert helper.process_identity(os.getpid(), os.getuid() + 1) is None
    assert helper.process_identity(2147483647, os.getuid()) is None
