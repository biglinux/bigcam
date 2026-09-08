"""Private, atomic control profiles keyed by stable camera identity."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
import re
import unicodedata

from core.camera_backend import CameraControl, CameraInfo
from utils import xdg
from utils.atomic_json import locked, read_object, write_object

log = logging.getLogger(__name__)


def _safe_filename(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError("Profile name must be text")
    name = unicodedata.normalize("NFC", name).strip()
    if not name or name in {".", ".."} or len(name.encode("utf-8")) > 180:
        raise ValueError("Invalid profile name")
    safe = re.sub(r"[^\w\-.]", "_", name)
    if safe.startswith("."):
        safe = "profile_" + safe.lstrip(".")
    return safe


def _directory(camera: CameraInfo) -> Path:
    root = Path(xdg.profiles_dir()).resolve()
    identity = camera.id or f"{camera.backend.value}:{camera.device_path}"
    directory = root / ("camera-" + hashlib.sha256(identity.encode("utf-8")).hexdigest())
    if directory.is_symlink():
        raise ValueError("Profile directory must not be a symbolic link")
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.resolve().parent != root:
        raise ValueError("Profile directory is outside the profile root")
    return directory


def _legacy_directory(camera: CameraInfo) -> Path | None:
    """Read old profiles only from a contained non-symlink name directory."""
    if not isinstance(camera.name, str):
        return None
    old_name = re.sub(r"[^\w\-.]", "_", camera.name)
    if old_name in {"", ".", ".."}:
        return None
    root = Path(xdg.profiles_dir()).resolve()
    directory = root / old_name
    if directory.is_symlink() or not directory.is_dir() or directory.resolve().parent != root:
        return None
    return directory


def _profile_path(camera: CameraInfo, profile_name: str) -> str:
    return str(_directory(camera) / f"{_safe_filename(profile_name)}.json")


def list_profiles(camera: CameraInfo) -> list[str]:
    names: set[str] = set()
    for directory in (_legacy_directory(camera), _directory(camera)):
        if directory is not None:
            for path in directory.glob("*.json"):
                if path.is_file() and not path.is_symlink():
                    names.add(path.stem)
    return sorted(names)


def save_profile(camera: CameraInfo, profile_name: str, controls: list[CameraControl]) -> str:
    path = _profile_path(camera, profile_name)
    data = {control.id: control.value for control in controls
            if not any(flag in (control.flags or "") for flag in ("read-only", "inactive"))}
    with locked(path):
        write_object(path, data)
    return path


def load_profile(camera: CameraInfo, profile_name: str) -> dict[str, object]:
    path = Path(_profile_path(camera, profile_name))
    if not path.exists():
        legacy = _legacy_directory(camera)
        if legacy is not None:
            path = legacy / f"{_safe_filename(profile_name)}.json"
    try:
        data = read_object(path)
        if any(not isinstance(key, str) or not isinstance(value, (str, int, float, bool, type(None)))
               for key, value in data.items()):
            raise ValueError("Invalid profile values")
        return data
    except (OSError, ValueError, UnicodeError):
        log.warning("Unable to read control profile", exc_info=True)
        return {}


def delete_profile(camera: CameraInfo, profile_name: str) -> bool:
    name = f"{_safe_filename(profile_name)}.json"
    removed = False
    for directory in (_directory(camera), _legacy_directory(camera)):
        if directory is None:
            continue
        path = directory / name
        with locked(path):
            if path.is_symlink():
                raise ValueError("Refusing symbolic-link profile")
            if path.is_file():
                path.unlink()
                removed = True
    return removed
