"""Atomic, process-safe settings with independent-instance merge semantics."""
from __future__ import annotations

from copy import deepcopy
import logging
import math
import os
from pathlib import Path
import threading
import uuid

from utils import xdg
from utils.atomic_json import locked, read_object, write_object

log = logging.getLogger(__name__)

_DEFAULTS: dict[str, object] = {
    # Window
    "window-width": 1100,
    "window-height": 700,
    "window-maximized": False,
    "sidebar-position": 420,
    # Preview
    "preferred-resolution": "",
    "fps-limit": 0,
    "mirror_preview": False,
    "capture-timer": 0,
    "grid_overlay": False,
    "overlay-opacity": 75,
    "controls-opacity": 90,
    "window-opacity": 50,
    # Photo
    "photo-directory": "",
    "photo-format": "jpg",
    "photo-name-pattern": "photo_{datetime}",
    # GPhoto2
    "gphoto2-bitrate": 5000,
    # General
    "show-welcome": True,
    "show-help-tooltips": True,
    "show_fps": True,
    "theme": "dark",
    "auto-start-preview": True,
    "hotplug_enabled": True,
    "last-camera-id": "",
    # Virtual camera
    "virtual-camera-enabled": False,
    "vcam-max-devices": 5,
    "vcam-name-template": "BigCam Virtual",
    "vcam-disabled-cameras": [],  # List of camera IDs where vcam is explicitly disabled
    # Pipeline
    "prefer-v4l2": True,
    # Recording
    "recording-video-codec": "h264",
    "recording-audio-codec": "opus",
    "recording-container": "mkv",
    "recording-video-bitrate": 8000,
    # IP Cameras (list serialised as JSON array)
    "ip_cameras": [],
    "reduce-motion": False,
    "auto-hide-controls": True,
    "resource-monitor-auto-optimize": False,
    # Resource monitor
    "resource-monitor-enabled": False,
    "resource-warnings-dismissed": [],
}


# Clamp persisted settings as well as values arriving through the UI.
_RANGES = {
    "window-width": (320, 16384), "window-height": (240, 16384),
    "sidebar-position": (200, 1200), "fps-limit": (0, 240),
    "capture-timer": (0, 60), "overlay-opacity": (0, 100),
    "controls-opacity": (20, 100), "window-opacity": (0, 100),
    "vcam-max-devices": (1, 8), "recording-video-bitrate": (500, 50000),
}


def _coerce(key: str, value: object, fallback: object) -> object:
    if isinstance(fallback, bool):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            word = value.strip().lower()
            if word in {"true", "1", "yes"}:
                return True
            if word in {"false", "0", "no", ""}:
                return False
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        return fallback
    if isinstance(fallback, (int, float)):
        try:
            number = float(value)
            if not math.isfinite(number):
                return fallback
            result = int(number) if isinstance(fallback, int) else number
        except (ValueError, TypeError, OverflowError):
            return fallback
        if key in _RANGES:
            low, high = _RANGES[key]
            result = max(low, min(high, result))
        return result
    if isinstance(fallback, (list, dict)):
        return deepcopy(value if isinstance(value, type(fallback)) else fallback)
    choices = {
        "theme": {"system", "light", "dark"},
        "recording-video-codec": {"h264", "h265", "vp9", "mjpeg"},
        "recording-audio-codec": {"opus", "aac", "mp3", "vorbis"},
        "recording-container": {"mkv", "mp4", "webm"},
        "preferred-resolution": {"", "480", "720", "1080", "2160"},
    }
    if key in choices and (not isinstance(value, str) or value not in choices[key]):
        return deepcopy(fallback)
    return value if isinstance(value, str) else deepcopy(fallback)


class SettingsManager:
    """Merge each write against the latest file under an advisory file lock.

    Readers invalidate their cache when the file inode/mtime/size changes. Lists
    and dictionaries returned to callers are copies, never shared mutable state.
    I/O failure is logged and returned by set(); the last good file is retained.
    """

    def __init__(self) -> None:
        self._path = os.path.join(xdg.config_dir(), "settings.json")
        self._data: dict[str, object] = {}
        self._signature = None
        self._lock = threading.RLock()
        self._load()

    def _stat_signature(self):
        try:
            st = os.stat(self._path, follow_symlinks=False)
            return st.st_ino, st.st_size, st.st_mtime_ns
        except OSError:
            return None

    def _load(self) -> None:
        with self._lock:
            signature = self._stat_signature()
            try:
                self._data = read_object(self._path)
            except (OSError, ValueError, UnicodeError):
                log.warning("Invalid settings; using defaults", exc_info=True)
                self._data = {}
            self._signature = signature

    def get(self, key: str, default: object = None) -> object:
        with self._lock:
            if self._stat_signature() != self._signature:
                self._load()
            fallback = default if default is not None else _DEFAULTS.get(key, "")
            return _coerce(key, self._data.get(key, fallback), fallback)

    def set(self, key: str, value: object) -> bool:
        return self.update({key: value})

    def update(self, changes: dict[str, object]) -> bool:
        """Apply multiple values in one transaction, without losing other keys."""
        if any(not isinstance(key, str) for key in changes):
            raise TypeError("Setting names must be strings")
        changes = deepcopy(changes)
        with self._lock:
            try:
                with locked(self._path):
                    try:
                        latest = read_object(self._path)
                    except (ValueError, UnicodeError):
                        # Preserve invalid user data for diagnosis before recovery.
                        path = Path(self._path)
                        if path.exists() and not path.is_symlink():
                            os.replace(path, path.with_name(f"settings.invalid-{uuid.uuid4().hex}.json"))
                        latest = {}
                    for key, value in changes.items():
                        latest[key] = _coerce(key, value, _DEFAULTS[key]) if key in _DEFAULTS else value
                    write_object(self._path, latest)
                    self._data = latest
                    self._signature = self._stat_signature()
                return True
            except (OSError, ValueError, TypeError):
                log.exception("Failed to save settings")
                return False
