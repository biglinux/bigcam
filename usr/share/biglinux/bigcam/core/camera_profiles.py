"""Camera profiles – save/load control presets per camera."""

from __future__ import annotations

import json
import os
import re

from core.camera_backend import CameraControl, CameraInfo
from utils import xdg


def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w\-.]", "_", name)


def _profile_path(camera: CameraInfo, profile_name: str) -> str:
    cam_dir = os.path.join(xdg.profiles_dir(), _safe_filename(camera.name))
    os.makedirs(cam_dir, exist_ok=True)
    return os.path.join(cam_dir, f"{_safe_filename(profile_name)}.json")


def list_profiles(camera: CameraInfo) -> list[str]:
    """Return profile names available for *camera*."""
    cam_dir = os.path.join(xdg.profiles_dir(), _safe_filename(camera.name))
    if not os.path.isdir(cam_dir):
        return []
    names: list[str] = []
    for f in sorted(os.listdir(cam_dir)):
        if f.endswith(".json"):
            names.append(f[:-5])
    return names


def save_profile(
    camera: CameraInfo, profile_name: str, controls: list[CameraControl]
) -> str:
    """Persist current control values. Returns the file path."""
    path = _profile_path(camera, profile_name)
    data = {c.id: c.value for c in controls}
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    return path


def load_profile(camera: CameraInfo, profile_name: str) -> dict[str, object]:
    """Return saved values as {control_id: value}."""
    path = _profile_path(camera, profile_name)
    if not os.path.isfile(path):
        return {}
    with open(path, "r") as f:
        return json.load(f)


def delete_profile(camera: CameraInfo, profile_name: str) -> bool:
    path = _profile_path(camera, profile_name)
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False
