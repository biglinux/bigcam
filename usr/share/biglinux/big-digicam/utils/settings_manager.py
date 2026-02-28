"""JSON-based settings persistence for Big DigiCam."""

import json
import os

from utils import xdg


_DEFAULTS: dict[str, object] = {
    # Window
    "window-width": 1100,
    "window-height": 700,
    "window-maximized": False,
    "sidebar-position": 420,
    # Preview
    "preferred-resolution": "",
    "fps-limit": 30,
    "mirror_preview": False,
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
    # Virtual camera
    "virtual-camera-enabled": False,
    # IP Cameras (list serialised as JSON array)
    "ip_cameras": [],
}


class SettingsManager:
    """Thread-safe JSON settings backed by ~/.config/big-digicam/settings.json."""

    def __init__(self) -> None:
        self._path = os.path.join(xdg.config_dir(), "settings.json")
        self._data: dict[str, object] = {}
        self._load()

    # -- public API ----------------------------------------------------------

    def get(self, key: str, default: object = None) -> object:
        fallback = default if default is not None else _DEFAULTS.get(key, "")
        value = self._data.get(key, fallback)
        # coerce to the same type as the fallback
        if isinstance(fallback, bool):
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
            self._data = {}

    def _save(self) -> None:
        try:
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, ensure_ascii=False)
        except Exception as exc:
            print(f"[SettingsManager] save error: {exc}")
