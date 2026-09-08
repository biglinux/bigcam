from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from constants import BackendType
from core.camera_backend import CameraInfo
from core import camera_profiles as profiles
from utils import atomic_json, xdg
from utils.settings_manager import SettingsManager
from utils.media_paths import reserve_media_path, reserve_named_path


def _set_from_process(index):
    assert SettingsManager().set(f"process-{index}", f"value-{index}")


def camera(name="Camera", ident="v4l2:serial-A"):
    return CameraInfo(ident, name, BackendType.V4L2, "/dev/video0")


def test_independent_instances_merge_and_invalidate():
    a, b = SettingsManager(), SettingsManager()
    assert a.set("theme", "light")
    assert b.set("show-welcome", False)
    assert a.get("show-welcome") is False
    assert b.get("theme") == "light"
    assert stat.S_IMODE(os.stat(a._path).st_mode) == 0o600


def test_cross_process_updates_are_not_lost():
    context = multiprocessing.get_context("spawn")
    processes = [context.Process(target=_set_from_process, args=(i,)) for i in range(12)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    manager = SettingsManager()
    assert all(manager.get(f"process-{i}") == f"value-{i}" for i in range(12))


def test_mutable_values_are_defensive_copies():
    a, b = SettingsManager(), SettingsManager()
    value = a.get("ip_cameras")
    value.append({"name": "camera", "url": "https://example.invalid/"})
    assert b.get("ip_cameras") == []
    assert a.get("ip_cameras") == []
    assert a.set("ip_cameras", value)
    value.clear()
    assert len(b.get("ip_cameras")) == 1


@pytest.mark.parametrize("raw", ['[]', 'null', 'false', '42', '"text"', '{broken', '{"x": NaN}'])
def test_malformed_roots_are_preserved_on_recovery(raw):
    path = Path(xdg.config_dir()) / "settings.json"
    path.write_text(raw)
    manager = SettingsManager()
    assert manager.get("theme") == "dark"
    assert manager.set("theme", "light")
    backups = list(path.parent.glob("settings.invalid-*.json"))
    assert len(backups) == 1
    assert backups[0].read_text() == raw
    assert atomic_json.read_object(path)["theme"] == "light"


def test_failed_write_does_not_truncate_previous_data(monkeypatch):
    manager = SettingsManager()
    assert manager.set("theme", "light")
    before = Path(manager._path).read_bytes()
    def fail(*args):
        raise OSError("simulated disk error")
    monkeypatch.setattr(atomic_json.os, "replace", fail)
    assert manager.set("theme", "dark") is False
    assert Path(manager._path).read_bytes() == before
    assert not list(Path(manager._path).parent.glob(".bigcam-*.tmp"))


def test_settings_refuse_symlinks(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text('{"theme":"light"}')
    (Path(xdg.config_dir()) / "settings.json").symlink_to(outside)
    manager = SettingsManager()
    assert manager.set("theme", "dark") is False
    assert json.loads(outside.read_text()) == {"theme": "light"}


@pytest.mark.parametrize("value,expected", [(999, 8), (-4, 1), (None, 5), ("bad", 5)])
def test_ranges_and_types(value, expected):
    manager = SettingsManager()
    assert manager.set("vcam-max-devices", value)
    assert manager.get("vcam-max-devices") == expected
    manager.set("show-welcome", "false")
    assert manager.get("show-welcome") is False


def test_profile_camera_dotdot_cannot_overwrite_settings():
    manager = SettingsManager()
    manager.set("theme", "light")
    before = Path(manager._path).read_bytes()
    target = profiles.save_profile(camera(".."), "settings", [SimpleNamespace(id="brightness", value=7, flags="")])
    assert Path(target).resolve().is_relative_to(Path(xdg.profiles_dir()).resolve())
    assert Path(manager._path).read_bytes() == before
    assert profiles.load_profile(camera(".."), "settings") == {"brightness": 7}
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o600


def test_identical_camera_names_do_not_share_new_profiles():
    profiles.save_profile(camera("Same", "A"), "test", [SimpleNamespace(id="a", value=1, flags="")])
    assert profiles.load_profile(camera("Same", "B"), "test") == {}


def test_profile_follows_physical_camera_after_video_node_renumbering():
    original = camera("Same", "v4l2:/dev/video0")
    original.extra["profile_id"] = "serial-A"
    moved = camera("Same", "v4l2:/dev/video2")
    moved.extra["profile_id"] = "serial-A"
    replacement = camera("Same", "v4l2:/dev/video0")
    replacement.extra["profile_id"] = "serial-B"
    profiles.save_profile(original, "portrait", [SimpleNamespace(id="brightness", value=7, flags="")])
    assert profiles.load_profile(moved, "portrait") == {"brightness": 7}
    assert profiles.load_profile(replacement, "portrait") == {}


@pytest.mark.parametrize("name", ["", ".", "..", "x" * 181])
def test_invalid_profile_names_fail(name):
    with pytest.raises(ValueError):
        profiles.save_profile(camera(), name, [])


def test_profile_symlink_rejected(tmp_path):
    path = Path(profiles._profile_path(camera(), "test"))
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    path.symlink_to(outside)
    with pytest.raises(ValueError):
        profiles.save_profile(camera(), "test", [])
    assert profiles.load_profile(camera(), "test") == {}
    assert outside.read_text() == "{}"


def test_profile_skips_non_writable_controls():
    controls = [SimpleNamespace(id="a", value=1, flags="read-only, inactive"),
                SimpleNamespace(id="b", value=2, flags=None)]
    profiles.save_profile(camera(), "test", controls)
    assert profiles.load_profile(camera(), "test") == {"b": 2}


def test_legacy_profiles_are_read_without_escaping():
    path = Path(xdg.profiles_dir()) / "Old_Camera"
    path.mkdir()
    (path / "default.json").write_text('{"brightness": 4}')
    assert profiles.load_profile(camera("Old Camera"), "default") == {"brightness": 4}


def test_concurrent_capture_names_are_unique(tmp_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        names = list(pool.map(lambda _: reserve_media_path(str(tmp_path), ".png"), range(128)))
    assert len(set(names)) == 128
    assert all(stat.S_IMODE(os.stat(p).st_mode) == 0o600 for p in names)


def test_named_capture_is_exclusive_and_confined(tmp_path):
    path = reserve_named_path(str(tmp_path), "photo.jpg")
    Path(path).write_bytes(b"original")
    with pytest.raises(FileExistsError):
        reserve_named_path(str(tmp_path), "photo.jpg")
    with pytest.raises(ValueError):
        reserve_named_path(str(tmp_path), "../photo.jpg")
    assert Path(path).read_bytes() == b"original"
