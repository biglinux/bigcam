"""IP camera backend – RTSP / HTTP streams."""

from __future__ import annotations

import subprocess
from typing import Any

from constants import BackendType, ControlCategory, ControlType
from core.camera_backend import CameraBackend, CameraControl, CameraInfo, VideoFormat
from utils.i18n import _


class IPBackend(CameraBackend):
    """Backend for RTSP / HTTP network cameras (manual configuration)."""

    def get_backend_type(self) -> BackendType:
        return BackendType.IP

    def is_available(self) -> bool:
        # Always available (relies on GStreamer which is already a dep)
        return True

    # -- detection -----------------------------------------------------------

    def detect_cameras(self) -> list[CameraInfo]:
        # IP cameras are configured manually; caller should provide them.
        return []

    def cameras_from_urls(self, entries: list[dict[str, str]]) -> list[CameraInfo]:
        """Build CameraInfo list from user-saved [{"name": ..., "url": ...}]."""
        cameras: list[CameraInfo] = []
        for entry in entries:
            url = entry.get("url", "")
            name = entry.get("name", url)
            if not url:
                continue
            cameras.append(CameraInfo(
                id=f"ip:{url}",
                name=name,
                backend=BackendType.IP,
                device_path=url,
                capabilities=["video"],
                extra={"url": url},
            ))
        return cameras

    # -- controls (none for basic IP) ----------------------------------------

    def get_controls(self, camera: CameraInfo) -> list[CameraControl]:
        return []

    def set_control(self, camera: CameraInfo, control_id: str, value: Any) -> bool:
        return False

    # -- gstreamer -----------------------------------------------------------

    def get_gst_source(self, camera: CameraInfo, fmt: VideoFormat | None = None) -> str:
        url = camera.extra.get("url", camera.device_path)
        if url.startswith("rtsp://"):
            return (
                f'rtspsrc location="{url}" latency=300 ! '
                "decodebin ! videoconvert"
            )
        # HTTP / MJPEG stream
        return (
            f'souphttpsrc location="{url}" ! '
            "decodebin ! videoconvert"
        )

    # -- photo ---------------------------------------------------------------

    def can_capture_photo(self) -> bool:
        return True

    def capture_photo(self, camera: CameraInfo, output_path: str) -> bool:
        """Snapshot via GStreamer one-frame pipeline."""
        url = camera.extra.get("url", camera.device_path)
        if url.startswith("rtsp://"):
            src = f'rtspsrc location="{url}" latency=300 ! decodebin'
        else:
            src = f'souphttpsrc location="{url}" ! decodebin'
        try:
            subprocess.run(
                [
                    "gst-launch-1.0", "-e",
                    *src.split(),
                    "!", "videoconvert",
                    "!", "jpegenc",
                    "!", "filesink", f"location={output_path}",
                ],
                capture_output=True,
                check=True,
                timeout=15,
            )
            import os
            return os.path.isfile(output_path)
        except Exception:
            return False
