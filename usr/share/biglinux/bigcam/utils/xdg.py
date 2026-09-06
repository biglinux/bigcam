"""XDG Base Directory paths for BigCam."""

import functools
import os
import subprocess

_APP = "bigcam"


def _ensure(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


@functools.cache
def _user_dir(kind: str, fallback: str) -> str:
    """Get XDG user directory via xdg-user-dir command."""
    try:
        result = subprocess.run(
            ["xdg-user-dir", kind],
            capture_output=True, text=True, timeout=3,
        )
        path = result.stdout.strip()
        if path and os.path.isabs(path):
            return path
    except (OSError, subprocess.TimeoutExpired):
        pass
    return os.path.expanduser(fallback)


def config_dir() -> str:
    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return _ensure(os.path.join(base, _APP))


def data_dir() -> str:
    base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    return _ensure(os.path.join(base, _APP))


def cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    return _ensure(os.path.join(base, _APP))


def photos_dir() -> str:
    pictures = _user_dir("PICTURES", "~/Pictures")
    return _ensure(os.path.join(pictures, "BigCam"))


def videos_dir() -> str:
    videos = _user_dir("VIDEOS", "~/Videos")
    return _ensure(os.path.join(videos, "BigCam"))


def profiles_dir() -> str:
    return _ensure(os.path.join(config_dir(), "profiles"))


def thumbs_dir() -> str:
    return _ensure(os.path.join(cache_dir(), "thumbs"))
