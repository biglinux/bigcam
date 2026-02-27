"""XDG Base Directory paths for Big DigiCam."""

import os

_APP = "big-digicam"


def _ensure(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


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
    pictures = os.environ.get(
        "XDG_PICTURES_DIR",
        os.path.expanduser("~/Pictures"),
    )
    return _ensure(os.path.join(pictures, "Big DigiCam"))


def videos_dir() -> str:
    videos = os.environ.get(
        "XDG_VIDEOS_DIR",
        os.path.expanduser("~/Videos"),
    )
    return _ensure(os.path.join(videos, "Big DigiCam"))


def profiles_dir() -> str:
    return _ensure(os.path.join(config_dir(), "profiles"))


def thumbs_dir() -> str:
    return _ensure(os.path.join(cache_dir(), "thumbs"))
