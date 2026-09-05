"""SettingsManager: type coercion, atomic persistence, thread-safety."""

from __future__ import annotations

import json
import threading


def test_defaults_are_returned_for_missing_keys(settings):
    assert settings.get("window-width") == 1100
    assert settings.get("virtual-camera-enabled") is True
    assert settings.get("vcam-disabled-cameras") == []


def test_bool_coercion_from_strings(settings):
    settings.set("mirror_preview", "true")
    assert settings.get("mirror_preview") is True
    settings.set("mirror_preview", "no")
    assert settings.get("mirror_preview") is False


def test_int_coercion_falls_back_on_garbage(settings):
    settings.set("fps-limit", "not-a-number")
    assert settings.get("fps-limit") == 0


def test_list_coercion_falls_back_on_wrong_type(settings):
    settings.set("ip_cameras", "oops")
    assert settings.get("ip_cameras") == []


def test_explicit_false_default_is_honoured(settings):
    # A caller passing default=False must not silently get the _DEFAULTS value.
    assert settings.get("resource-monitor-auto-optimize", False) is False


def test_roundtrip_persists_to_disk(settings):
    settings.set("last-camera-id", "v4l2:/dev/video7")
    with open(settings._path, encoding="utf-8") as fh:
        raw = json.load(fh)
    assert raw["last-camera-id"] == "v4l2:/dev/video7"


def test_save_is_atomic_no_tmp_left_behind(settings, tmp_path):
    import os

    settings.set("theme", "light")
    leftovers = [p for p in os.listdir(os.path.dirname(settings._path))
                 if p.endswith(".tmp")]
    assert leftovers == []


def test_corrupt_file_does_not_crash(settings):
    with open(settings._path, "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")
    settings._load()
    assert settings.get("window-width") == 1100


def test_concurrent_writes_do_not_corrupt(settings):
    def writer(n: int) -> None:
        for i in range(40):
            settings.set(f"k{n}", i)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
        assert not t.is_alive()

    with open(settings._path, encoding="utf-8") as fh:
        json.load(fh)  # must still be valid JSON
