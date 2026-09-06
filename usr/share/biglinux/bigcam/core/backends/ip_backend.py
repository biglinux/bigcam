"""IP camera backend – RTSP / HTTP streams."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any

from constants import BackendType
from core.camera_backend import CameraBackend, CameraControl, CameraInfo, VideoFormat

log = logging.getLogger(__name__)

ALLOWED_SCHEMES = ("rtsp://", "rtsps://", "http://", "https://")

# The URL is interpolated into a gst_parse_launch description, where a quote,
# a backslash or whitespace ends the location= value and everything after it
# is read as more pipeline.  Only the characters RFC 3986 allows in a URL are
# accepted; anything else has to be percent-encoded by the user.
_URL_SAFE = re.compile(r"^[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+$")


def validate_stream_url(url: str) -> str:
    """Return *url* trimmed if it is safe to put in a pipeline, else ""."""
    url = (url or "").strip()
    if not url or not url.lower().startswith(ALLOWED_SCHEMES):
        return ""
    if not _URL_SAFE.match(url):
        log.warning("Rejecting stream URL with unsafe characters")
        return ""
    return url


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
            url = validate_stream_url(entry.get("url", ""))
            name = entry.get("name") or url
            if not url:
                log.warning("Skipping saved IP camera with an invalid URL")
                continue
            cameras.append(
                CameraInfo(
                    id=f"ip:{url}",
                    name=name,
                    backend=BackendType.IP,
                    device_path=url,
                    capabilities=["video"],
                    extra={"url": url},
                )
            )
        return cameras

    # -- controls (none for basic IP) ----------------------------------------

    def get_controls(self, camera: CameraInfo) -> list[CameraControl]:
        return []

    def set_control(self, camera: CameraInfo, control_id: str, value: Any) -> bool:
        return False

    # -- gstreamer -----------------------------------------------------------

    def get_gst_source(self, camera: CameraInfo, fmt: VideoFormat | None = None) -> str:
        url = validate_stream_url(camera.extra.get("url", camera.device_path))
        if not url:
            return ""
        if url.lower().startswith(("rtsp://", "rtsps://")):
            return f'rtspsrc location="{url}" latency=300 ! decodebin ! videoconvert'
        # HTTP / MJPEG stream
        return f'souphttpsrc location="{url}" ! decodebin ! videoconvert'

    # -- photo ---------------------------------------------------------------

    def can_capture_photo(self) -> bool:
        return True

    def capture_photo(self, camera: CameraInfo, output_path: str) -> bool:
        """Snapshot via GStreamer one-frame pipeline."""
        url = validate_stream_url(camera.extra.get("url", camera.device_path))
        if not url:
            return False
        if url.lower().startswith(("rtsp://", "rtsps://")):
            src_args = ["rtspsrc", f"location={url}", "latency=300", "!", "decodebin"]
        else:
            src_args = ["souphttpsrc", f"location={url}", "!", "decodebin"]
        try:
            subprocess.run(
                [
                    "gst-launch-1.0",
                    "-e",
                    *src_args,
                    "!",
                    "videoconvert",
                    "!",
                    "jpegenc",
                    "!",
                    "filesink",
                    f"location={output_path}",
                ],
                capture_output=True,
                check=True,
                timeout=15,
            )
            return os.path.isfile(output_path)
        except Exception:
            return False
