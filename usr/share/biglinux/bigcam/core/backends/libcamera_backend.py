"""libcamera backend – CSI / ISP cameras (Raspberry Pi, Intel IPU6, etc.)."""

from __future__ import annotations

import re
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
        for cmd in ("cam", "libcamera-hello"):
            try:
                subprocess.run([cmd, "--version"], capture_output=True, timeout=5)
                return True
            except (
                FileNotFoundError,
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
            ):
                continue
        return False

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
                    cameras.append(
                        CameraInfo(
                            id=f"libcamera:{idx}",
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
        # libcamera CLI does not expose a rich control listing;
        # provide the most common ones as GStreamer element properties.
        return [
            CameraControl(
                id="brightness",
                name=_("Brightness"),
                category=ControlCategory.IMAGE,
                control_type=ControlType.INTEGER,
                value=0,
                default=0,
                minimum=-100,
                maximum=100,
                step=1,
            ),
            CameraControl(
                id="contrast",
                name=_("Contrast"),
                category=ControlCategory.IMAGE,
                control_type=ControlType.INTEGER,
                value=100,
                default=100,
                minimum=0,
                maximum=200,
                step=1,
            ),
            CameraControl(
                id="saturation",
                name=_("Saturation"),
                category=ControlCategory.IMAGE,
                control_type=ControlType.INTEGER,
                value=100,
                default=100,
                minimum=0,
                maximum=200,
                step=1,
            ),
            CameraControl(
                id="awb-mode",
                name=_("Auto White Balance"),
                category=ControlCategory.WHITE_BALANCE,
                control_type=ControlType.MENU,
                value="auto",
                default="auto",
                choices=[
                    "auto",
                    "incandescent",
                    "tungsten",
                    "fluorescent",
                    "indoor",
                    "daylight",
                    "cloudy",
                    "custom",
                ],
            ),
            CameraControl(
                id="exposure-mode",
                name=_("Exposure Mode"),
                category=ControlCategory.EXPOSURE,
                control_type=ControlType.MENU,
                value="normal",
                default="normal",
                choices=["normal", "short", "long", "custom"],
            ),
        ]

    def set_control(self, camera: CameraInfo, control_id: str, value: Any) -> bool:
        # Stored in extra for pipeline rebuild
        camera.extra[f"ctrl_{control_id}"] = value
        return True

    # -- gstreamer -----------------------------------------------------------

    def get_gst_source(self, camera: CameraInfo, fmt: VideoFormat | None = None) -> str:
        cam_name = camera.device_path
        src = f"libcamerasrc camera-name={cam_name}"
        if fmt:
            caps = f"video/x-raw,width={fmt.width},height={fmt.height}"
            if fmt.fps:
                best = int(max(fmt.fps))
                caps += f",framerate={best}/1"
            return f"{src} ! {caps}"
        return src

    # -- photo ---------------------------------------------------------------

    def can_capture_photo(self) -> bool:
        return True

    def capture_photo(self, camera: CameraInfo, output_path: str) -> bool:
        try:
            subprocess.run(
                ["libcamera-still", "-o", output_path, "--nopreview", "-t", "1"],
                capture_output=True,
                check=True,
                timeout=15,
            )
            import os

            return os.path.isfile(output_path)
        except Exception:
            return False
