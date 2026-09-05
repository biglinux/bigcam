"""Shared pytest fixtures for the BigCam test-suite.

The suite is designed to run head-less and **without any camera, microphone
or kernel module**.  Anything that would touch real hardware is either
monkey-patched or guarded behind the ``hardware`` marker.
"""

from __future__ import annotations

import os
import sys
import types

import pytest

# --------------------------------------------------------------------------
# Make the application package importable and keep every XDG path inside a
# throw-away directory so tests never touch the developer's real config,
# photos or videos.
# --------------------------------------------------------------------------

APP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "usr", "share", "biglinux", "bigcam")
)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)


@pytest.fixture(autouse=True, scope="session")
def _gst_init():
    """GStreamer must be initialised before any Gst.* call in the suite."""
    import gi

    gi.require_version("Gst", "1.0")
    from gi.repository import Gst

    if not Gst.is_initialized():
        Gst.init(None)
    yield


@pytest.fixture(autouse=True, scope="session")
def _isolated_xdg(tmp_path_factory):
    """Redirect every XDG base dir into a temporary tree for the whole run."""
    root = tmp_path_factory.mktemp("xdg")
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_STATE_HOME", "state"),
    ):
        path = root / sub
        path.mkdir(parents=True, exist_ok=True)
        os.environ[var] = str(path)
    # xdg-user-dir would point at the real ~/Pictures; force the fallback.
    os.environ["HOME"] = str(root)
    yield root


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """A SettingsManager backed by an empty temporary config dir."""
    from utils import xdg
    from utils.settings_manager import SettingsManager

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setattr(xdg, "config_dir", lambda: str(cfg))
    return SettingsManager()


@pytest.fixture
def no_subprocess(monkeypatch):
    """Fail loudly if code under test spawns a real process.

    Returns a list that records every attempted command, so a test can also
    use it as a spy instead of a hard guard.
    """
    import subprocess

    calls: list[list[str]] = []

    def _fake_run(args, *a, **kw):
        calls.append(list(args) if isinstance(args, (list, tuple)) else [str(args)])
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def _fake_popen(args, *a, **kw):
        calls.append(list(args) if isinstance(args, (list, tuple)) else [str(args)])
        proc = types.SimpleNamespace()
        proc.pid = -1
        proc.stdin = None
        proc.stdout = None
        proc.poll = lambda: 0
        proc.wait = lambda timeout=None: 0
        proc.terminate = lambda: None
        proc.kill = lambda: None
        return proc

    monkeypatch.setattr(subprocess, "run", _fake_run)
    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    return calls


@pytest.fixture
def bgr_frame():
    """A small deterministic BGR frame for image-processing tests."""
    np = pytest.importorskip("numpy")
    h, w = 48, 64
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[:, : w // 2] = (40, 60, 80)
    frame[:, w // 2 :] = (160, 170, 180)
    return frame
