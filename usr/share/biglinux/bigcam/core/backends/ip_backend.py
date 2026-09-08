"""IP camera backend – RTSP / HTTP streams."""

from __future__ import annotations

import logging
import os
import subprocess
from urllib.parse import urlsplit
from utils.urls import camera_url, camera_url_id, public_camera_name, gst_quote
from typing import Any

from constants import BackendType
from core.camera_backend import CameraBackend, CameraControl, CameraInfo, VideoFormat

log = logging.getLogger(__name__)


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
        cameras = []
        for entry in entries:
            try:
                url = camera_url(entry["url"])
                name = entry.get("name") or public_camera_name(url)
                # Legacy configurations commonly stored the credential-bearing URL as a name.
                if name == entry["url"] or "://" in name:
                    name = public_camera_name(url)
            except (KeyError, TypeError, ValueError):
                log.warning("Ignoring an invalid camera configuration")
                continue
            cameras.append(CameraInfo(id=camera_url_id(url), name=name, backend=BackendType.IP,
                                      device_path=url, capabilities=["video"], extra={"url": url}))
        return cameras

    # -- controls (none for basic IP) ----------------------------------------

    def get_controls(self, camera: CameraInfo) -> list[CameraControl]:
        return []

    def set_control(self, camera: CameraInfo, control_id: str, value: Any) -> bool:
        return False

    # -- gstreamer -----------------------------------------------------------

    def get_gst_source(self, camera: CameraInfo, fmt: VideoFormat | None = None) -> str:
        url = camera_url(camera.extra.get("url", camera.device_path))
        if urlsplit(url).scheme in {"rtsp", "rtsps"}:
            return f"rtspsrc location={gst_quote(url)} latency=150 ! decodebin ! videoconvert"
        return f"souphttpsrc location={gst_quote(url)} ! decodebin ! videoconvert"

    # -- photo ---------------------------------------------------------------

    def can_capture_photo(self) -> bool:
        return True

    def capture_photo(self, camera: CameraInfo, output_path: str) -> bool:
        """jpegenc snapshot sends EOS after the first frame; never timeout to finish."""
        try:
            url = camera_url(camera.extra.get("url", camera.device_path))
            source = "rtspsrc" if urlsplit(url).scheme in {"rtsp", "rtsps"} else "souphttpsrc"
            subprocess.run(["gst-launch-1.0", "-e", source, f"location={gst_quote(url)}",
                            "!", "decodebin", "!", "videoconvert", "!", "jpegenc", "snapshot=true",
                            "!", "filesink", f"location={gst_quote(output_path)}"],
                           capture_output=True, check=True, timeout=15)
            return os.path.isfile(output_path) and os.path.getsize(output_path) > 0
        except (OSError, ValueError, subprocess.SubprocessError):
            log.warning("Network snapshot failed")
            return False
