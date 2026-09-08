"""libcamera backend – CSI / ISP cameras (Raspberry Pi, Intel IPU6, etc.)."""

from __future__ import annotations

import re
import shutil
from utils.urls import gst_quote
from utils.video_formats import frame_rate
import subprocess
from typing import Any

from constants import BackendType, ControlCategory, ControlType
from core.camera_backend import CameraBackend, CameraControl, CameraInfo, VideoFormat
from utils.i18n import _


class LibcameraBackend(CameraBackend):
    """Backend for libcamera-supported cameras."""

    def get_backend_type(self) -> BackendType:
        return BackendType.LIBCAMERA

    def is_available(self) -> bool:
        return shutil.which("cam") is not None

    # -- detection -----------------------------------------------------------

    def detect_cameras(self) -> list[CameraInfo]:
        cameras: list[CameraInfo] = []
        try:
            result = subprocess.run(
                ["cam", "--list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return cameras

            for line in result.stdout.splitlines():
                # Example: "1: Internal front camera (/base/soc/i2c0/imx219)"
                m = re.match(r"\s*(\d+):\s+(.+)\s+\((.+)\)", line)
                if m:
                    idx = m.group(1)
                    name = m.group(2).strip()
                    path = m.group(3).strip()
                    # Skip USB/UVC cameras — V4L2 backend handles those
                    if "usb" in path.lower() or "uvc" in path.lower():
                        continue
                    cameras.append(
                        CameraInfo(
                            id=f"libcamera:{path}",
                            name=name,
                            backend=BackendType.LIBCAMERA,
                            device_path=path,
                            capabilities=["video", "photo"],
                            extra={"index": idx},
                        )
                    )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return cameras

    # -- controls (limited at CLI level) -------------------------------------

    def get_controls(self, camera: CameraInfo) -> list[CameraControl]:
        # cam does not provide a stable per-camera read/write control API here.
        # Do not display fabricated hardware controls that never reach the device.
        # The independent Effects page remains available for software adjustments.
        return []

    def set_control(self, camera: CameraInfo, control_id: str, value: Any) -> bool:
        return False

    # -- gstreamer -----------------------------------------------------------

    def get_gst_source(self, camera: CameraInfo, fmt: VideoFormat | None = None) -> str:
        cam_name = camera.device_path
        src = f"libcamerasrc camera-name={gst_quote(cam_name)}"
        if fmt:
            caps = f"video/x-raw,width={fmt.width},height={fmt.height}"
            if fmt.fps:
                caps += ",framerate=" + frame_rate(max(fmt.fps))
            return f"{src} ! {caps}"
        return src

    # -- photo ---------------------------------------------------------------

    def can_capture_photo(self) -> bool:
        return True

    def capture_photo(self, camera: CameraInfo, output_path: str) -> bool:
        try:
            subprocess.run(["gst-launch-1.0", "-e", "libcamerasrc",
                            f"camera-name={gst_quote(camera.device_path)}", "!", "videoconvert",
                            "!", "jpegenc", "snapshot=true", "!", "filesink",
                            f"location={gst_quote(output_path)}"], capture_output=True, check=True, timeout=15)
            import os
            return os.path.isfile(output_path) and os.path.getsize(output_path) > 0
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
