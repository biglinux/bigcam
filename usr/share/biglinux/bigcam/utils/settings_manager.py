"""JSON-based settings persistence for BigCam."""

import json
import logging
import os
import tempfile
import threading

from utils import xdg

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
    # Photo
    "photo-directory": "",
    "photo-format": "jpg",
    "photo-name-pattern": "foto_{datetime}",
    # GPhoto2
    "gphoto2-bitrate": 5000,
    # General
    "show-welcome": True,
    "show_fps": True,
    "theme": "system",
    "auto-start-preview": True,
    "hotplug_enabled": True,
    "last-camera-id": "",
    # Virtual camera
    "virtual-camera-enabled": False,
    # IP Cameras (list serialised as JSON array)
    "ip_cameras": [],
}

_BOOL_TRUE = {"true", "1", "yes"}
_BOOL_FALSE = {"false", "0", "no", ""}


class SettingsManager:
    """Thread-safe JSON settings backed by ~/.config/bigcam/settings.json."""

    def __init__(self) -> None:
        self._path = os.path.join(xdg.config_dir(), "settings.json")
        self._data: dict[str, object] = {}
        self._lock = threading.Lock()
        self._load()

    # -- public API ----------------------------------------------------------

    def get(self, key: str, default: object = None) -> object:
        fallback = default if default is not None else _DEFAULTS.get(key, "")
        value = self._data.get(key, fallback)
        # coerce to the same type as the fallback
        if isinstance(fallback, bool):
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                low = value.lower()
                if low in _BOOL_TRUE:
                    return True
                if low in _BOOL_FALSE:
                    return False
            return bool(value)
        if isinstance(fallback, int):
            try:
                return int(value)
            except (ValueError, TypeError):
                return fallback
        if isinstance(fallback, float):
            try:
                return float(value)
            except (ValueError, TypeError):
                return fallback
        return str(value) if value is not None else ""

    def set(self, key: str, value: object) -> None:
        with self._lock:
            self._data[key] = value
            self._save()

    # -- persistence ---------------------------------------------------------

    def _load(self) -> None:
        if not os.path.isfile(self._path):
            self._data = {}
            return
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                self._data = json.load(fh)
        except Exception:
            log.warning("Failed to load settings from %s", self._path, exc_info=True)
            self._data = {}

    def _save(self) -> None:
        try:
            dir_path = os.path.dirname(self._path)
            fd, tmp = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh, indent=2, ensure_ascii=False)
                os.replace(tmp, self._path)
            except BaseException:
                os.unlink(tmp)
                raise
        except Exception as exc:
            log.error("Settings save error: %s", exc)
