"""Camera Manager – detects, tracks and switches between cameras from all backends."""

from __future__ import annotations

import json
import subprocess
import threading
from typing import Any, Callable

from gi.repository import GLib, GObject

from constants import BackendType
from core.camera_backend import CameraBackend, CameraControl, CameraInfo, VideoFormat
from core.backends.v4l2_backend import V4L2Backend
from core.backends.gphoto2_backend import GPhoto2Backend
from core.backends.libcamera_backend import LibcameraBackend
from core.backends.pipewire_backend import PipeWireBackend
from core.backends.ip_backend import IPBackend
from utils.i18n import _


class CameraManager(GObject.Object):
    """Orchestrates camera detection across all backends with hotplug support."""

    __gsignals__ = {
        "cameras-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "camera-error": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }

    def __init__(self) -> None:
        super().__init__()
        self._backends: list[CameraBackend] = []
        self._cameras: list[CameraInfo] = []
        self._detecting = False
        self._hotplug_timer: int | None = None
        self._last_lsusb: str = ""

        self._register_backends()

    # -- backend registration ------------------------------------------------

    def _register_backends(self) -> None:
        candidates: list[CameraBackend] = [
            V4L2Backend(),
            GPhoto2Backend(),
            LibcameraBackend(),
            PipeWireBackend(),
            IPBackend(),
        ]
        for b in candidates:
            try:
                if b.is_available():
                    self._backends.append(b)
            except Exception:
                pass

    @property
    def cameras(self) -> list[CameraInfo]:
        return list(self._cameras)

    @property
    def available_backends(self) -> list[BackendType]:
        return [b.get_backend_type() for b in self._backends]

    def get_backend(self, backend_type: BackendType) -> CameraBackend | None:
        for b in self._backends:
            if b.get_backend_type() == backend_type:
                return b
        return None

    # -- detection -----------------------------------------------------------

    def detect_cameras_async(self) -> None:
        """Run detection on all backends in a background thread."""
        if self._detecting:
            return
        self._detecting = True

        def _worker() -> None:
            all_cameras: list[CameraInfo] = []
            seen_ids: set[str] = set()
            for b in self._backends:
                if b.get_backend_type() == BackendType.IP:
                    continue  # IP cameras are added manually
                try:
                    found = b.detect_cameras()
                    for cam in found:
                        if cam.id not in seen_ids:
                            seen_ids.add(cam.id)
                            all_cameras.append(cam)
                except Exception as exc:
                    GLib.idle_add(self.emit, "camera-error", str(exc))
            self._detecting = False
            GLib.idle_add(self._on_detection_done, all_cameras)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_detection_done(self, cameras: list[CameraInfo]) -> bool:
        old_ids = {c.id for c in self._cameras}
        new_ids = {c.id for c in cameras}
        self._cameras = cameras
        if old_ids != new_ids:
            self.emit("cameras-changed")
        return False

    def add_ip_cameras(self, entries: list[dict[str, str]]) -> None:
        """Add manually-configured IP cameras."""
        backend = self.get_backend(BackendType.IP)
        if not isinstance(backend, IPBackend):
            return
        ip_cams = backend.cameras_from_urls(entries)
        # Remove old IP cameras
        self._cameras = [c for c in self._cameras if c.backend != BackendType.IP]
        self._cameras.extend(ip_cams)
        self.emit("cameras-changed")

    # -- controls proxy ------------------------------------------------------

    def get_controls(self, camera: CameraInfo) -> list[CameraControl]:
        backend = self.get_backend(camera.backend)
        if backend:
            return backend.get_controls(camera)
        return []

    def set_control(self, camera: CameraInfo, control_id: str, value: Any) -> bool:
        backend = self.get_backend(camera.backend)
        if backend:
            return backend.set_control(camera, control_id, value)
        return False

    def reset_all_controls(self, camera: CameraInfo, controls: list[CameraControl]) -> None:
        backend = self.get_backend(camera.backend)
        if backend:
            backend.reset_all_controls(camera, controls)

    # -- gstreamer proxy -----------------------------------------------------

    def get_gst_source(self, camera: CameraInfo, fmt: VideoFormat | None = None) -> str:
        backend = self.get_backend(camera.backend)
        if backend:
            return backend.get_gst_source(camera, fmt)
        return ""

    # -- photo proxy ---------------------------------------------------------

    def can_capture_photo(self, camera: CameraInfo) -> bool:
        backend = self.get_backend(camera.backend)
        return backend.can_capture_photo() if backend else False

    def capture_photo(self, camera: CameraInfo, output_path: str) -> bool:
        backend = self.get_backend(camera.backend)
        return backend.capture_photo(camera, output_path) if backend else False

    # -- hotplug polling -----------------------------------------------------

    def start_hotplug(self, interval_ms: int = 5000) -> None:
        if self._hotplug_timer is None:
            self._hotplug_timer = GLib.timeout_add(interval_ms, self._poll_hotplug)

    def stop_hotplug(self) -> None:
        if self._hotplug_timer is not None:
            GLib.source_remove(self._hotplug_timer)
            self._hotplug_timer = None

    def _poll_hotplug(self) -> bool:
        if self._detecting:
            return True

        def _check_usb() -> None:
            try:
                result = subprocess.run(["lsusb"], capture_output=True, text=True)
                current = result.stdout
                if current != self._last_lsusb:
                    self._last_lsusb = current
                    GLib.idle_add(self.detect_cameras_async)
            except Exception:
                pass

        threading.Thread(target=_check_usb, daemon=True).start()
        return True
