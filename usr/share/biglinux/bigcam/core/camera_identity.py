"""Conservative deduplication: equal labels never prove equal cameras."""
from __future__ import annotations
import os
from constants import BackendType

_PRIORITY = {BackendType.V4L2: 0, BackendType.GPHOTO2: 1,
             BackendType.LIBCAMERA: 2, BackendType.PIPEWIRE: 3}
MANUAL_BACKENDS = {BackendType.IP, BackendType.PHONE, BackendType.SCRCPY, BackendType.AIRPLAY}


def identity(camera):
    physical = camera.extra.get("physical_id")
    if physical:
        return "physical", str(physical)
    path = camera.extra.get("api.v4l2.path", camera.device_path)
    if isinstance(path, str) and path.startswith("/dev/video"):
        return "device", os.path.realpath(path)
    return "id", camera.id


def unique_cameras(cameras):
    found = {}
    for camera in cameras:
        key = identity(camera)
        old = found.get(key)
        if old is None or _PRIORITY.get(camera.backend, 99) < _PRIORITY.get(old.backend, 99):
            found[key] = camera
    return sorted(found.values(), key=lambda c: (_PRIORITY.get(c.backend, 99), c.id))
