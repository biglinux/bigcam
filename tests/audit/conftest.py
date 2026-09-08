"""Audit regression tests use only disposable user data, never real devices."""
from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "usr/share/biglinux/bigcam"))

@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for name, directory in [("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                            ("XDG_DATA_HOME", "data"), ("XDG_STATE_HOME", "state")]:
        monkeypatch.setenv(name, str(tmp_path / directory))
    from utils import xdg
    xdg._user_dir.cache_clear()
    monkeypatch.setattr(xdg, "_user_dir", lambda kind, fallback: str(tmp_path / kind))
