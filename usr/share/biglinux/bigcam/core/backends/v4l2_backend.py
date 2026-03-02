"""V4L2 backend – covers 95%+ of USB/UVC webcams."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from typing import Any

from constants import BackendType, ControlCategory, ControlType
from core.camera_backend import CameraBackend, CameraControl, CameraInfo, VideoFormat
from utils.i18n import _

log = logging.getLogger(__name__)

# Human-friendly labels for V4L2 control IDs
_CONTROL_LABELS: dict[str, str] = {
    "brightness": _("Brightness"),
    "contrast": _("Contrast"),
    "saturation": _("Saturation"),
    "hue": _("Hue"),
    "sharpness": _("Sharpness"),
    "gamma": _("Gamma"),
    "white_balance_automatic": _("Auto White Balance"),
    "white_balance_temperature": _("White Balance Temperature"),
    "gain": _("Gain"),
    "exposure_auto": _("Auto Exposure"),
    "exposure_absolute": _("Exposure Time"),
    "exposure_auto_priority": _("Exposure Auto Priority"),
    "focus_auto": _("Auto Focus"),
    "focus_absolute": _("Focus Distance"),
    "zoom_absolute": _("Zoom"),
    "pan_absolute": _("Pan"),
    "tilt_absolute": _("Tilt"),
    "backlight_compensation": _("Backlight Compensation"),
    "power_line_frequency": _("Power Line Frequency"),
    "auto_exposure_bias": _("Exposure Bias"),
    "white_balance_auto_preset": _("WB Preset"),
    "image_stabilization": _("Image Stabilization"),
    "iso_sensitivity": _("ISO Sensitivity"),
    "iso_sensitivity_auto": _("Auto ISO"),
    "scene_mode": _("Scene Mode"),
    "3a_lock": _("3A Lock"),
    "led1_mode": _("LED Mode"),
    "led1_frequency": _("LED Frequency"),
}

_CATEGORY_MAP: dict[str, ControlCategory] = {
    "brightness": ControlCategory.IMAGE,
    "contrast": ControlCategory.IMAGE,
    "saturation": ControlCategory.IMAGE,
    "hue": ControlCategory.IMAGE,
    "sharpness": ControlCategory.IMAGE,
    "gamma": ControlCategory.ADVANCED,
    "white_balance_automatic": ControlCategory.WHITE_BALANCE,
    "white_balance_temperature": ControlCategory.WHITE_BALANCE,
    "white_balance_auto_preset": ControlCategory.WHITE_BALANCE,
    "gain": ControlCategory.EXPOSURE,
    "exposure_auto": ControlCategory.EXPOSURE,
    "exposure_absolute": ControlCategory.EXPOSURE,
    "exposure_auto_priority": ControlCategory.EXPOSURE,
    "auto_exposure_bias": ControlCategory.EXPOSURE,
    "focus_auto": ControlCategory.FOCUS,
    "focus_absolute": ControlCategory.FOCUS,
    "zoom_absolute": ControlCategory.FOCUS,
    "pan_absolute": ControlCategory.ADVANCED,
    "tilt_absolute": ControlCategory.ADVANCED,
    "backlight_compensation": ControlCategory.EXPOSURE,
    "power_line_frequency": ControlCategory.ADVANCED,
    "image_stabilization": ControlCategory.ADVANCED,
    "iso_sensitivity": ControlCategory.EXPOSURE,
    "iso_sensitivity_auto": ControlCategory.EXPOSURE,
    "scene_mode": ControlCategory.ADVANCED,
    "led1_mode": ControlCategory.ADVANCED,
    "led1_frequency": ControlCategory.ADVANCED,
}


class V4L2Backend(CameraBackend):
    """Backend for Video4Linux2 webcams (UVC, etc.)."""

    def get_backend_type(self) -> BackendType:
        return BackendType.V4L2

    def is_available(self) -> bool:
        try:
            subprocess.run(
                ["v4l2-ctl", "--version"],
                capture_output=True,
                check=True,
            )
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            return False

    # -- detection -----------------------------------------------------------

    def detect_cameras(self) -> list[CameraInfo]:
        cameras: list[CameraInfo] = []
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--list-devices"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return cameras
            cameras = self._parse_devices(result.stdout)
        except Exception:
            pass
        return cameras

    def _parse_devices(self, output: str) -> list[CameraInfo]:
        cameras: list[CameraInfo] = []
        blocks = re.split(r"\n(?=\S)", output.strip())
        for block in blocks:
            lines = block.strip().splitlines()
            if len(lines) < 2:
                continue
            header = lines[0].rstrip(":")
            # Skip v4l2loopback virtual devices and the proxy ones created by the script
            if "v4l2loopback" in header.lower() or "loopback" in header.lower() or "(v4l2)" in header.lower():
                continue
            devs = [l.strip() for l in lines[1:] if l.strip().startswith("/dev/video")]
            if not devs:
                continue
            # Use the first /dev/videoX as primary
            device = devs[0]
            # Verify it has capture capability
            if not self._is_capture_device(device):
                continue
            cam = CameraInfo(
                id=f"v4l2:{device}",
                name=header.split("(")[0].strip(),
                backend=BackendType.V4L2,
                device_path=device,
                capabilities=["video", "controls"],
                formats=self._get_formats(device),
            )
            # Check if photo capture is achievable (always yes for v4l2 via gstreamer snapshot)
            cam.capabilities.append("photo")
            cameras.append(cam)
        return cameras

    def _is_capture_device(self, device: str) -> bool:
        try:
            result = subprocess.run(
                ["v4l2-ctl", "-d", device, "--info"],
                capture_output=True,
                text=True,
            )
            return "Video Capture" in result.stdout
        except Exception:
            return False

    def _get_formats(self, device: str) -> list[VideoFormat]:
        formats: list[VideoFormat] = []
        try:
            result = subprocess.run(
                ["v4l2-ctl", "-d", device, "--list-formats-ext"],
                capture_output=True,
                text=True,
            )
            current_fmt = ""
            current_desc = ""
            for line in result.stdout.splitlines():
                fmt_match = re.match(r"\s+\[\d+\]:\s+'(\w+)'\s+\((.+)\)", line)
                if fmt_match:
                    current_fmt = fmt_match.group(1)
                    current_desc = fmt_match.group(2).strip()
                    continue
                size_match = re.match(r"\s+Size:\s+\w+\s+(\d+)x(\d+)", line)
                if size_match and current_fmt:
                    w, h = int(size_match.group(1)), int(size_match.group(2))
                    fps_list: list[float] = []
                    continue
                fps_match = re.match(r"\s+Interval:.*\((\d+\.?\d*)\s+fps\)", line)
                if fps_match:
                    fps_list.append(float(fps_match.group(1)))
                    # We'll append when the next size/format line appears
                    # For now, accumulate
                elif line.strip() == "" or re.match(r"\s+(Size|\[\d)", line):
                    pass

            # Re-parse more robustly
            formats = self._parse_formats_ext(result.stdout)
        except Exception:
            pass
        return formats

    def _parse_formats_ext(self, output: str) -> list[VideoFormat]:
        formats: list[VideoFormat] = []
        current_fmt = ""
        current_desc = ""
        current_w = 0
        current_h = 0
        fps_list: list[float] = []

        for line in output.splitlines():
            fmt_match = re.match(r"\s+\[\d+\]:\s+'(\w+)'\s+\((.+)\)", line)
            if fmt_match:
                # Save previous if pending
                if current_fmt and current_w and fps_list:
                    formats.append(VideoFormat(
                        width=current_w, height=current_h,
                        fps=list(fps_list), pixel_format=current_fmt,
                        description=current_desc,
                    ))
                current_fmt = fmt_match.group(1)
                current_desc = fmt_match.group(2).strip()
                current_w = current_h = 0
                fps_list = []
                continue

            size_match = re.match(r"\s+Size:\s+\w+\s+(\d+)x(\d+)", line)
            if size_match:
                # Save previous size if pending
                if current_fmt and current_w and fps_list:
                    formats.append(VideoFormat(
                        width=current_w, height=current_h,
                        fps=list(fps_list), pixel_format=current_fmt,
                        description=current_desc,
                    ))
                current_w = int(size_match.group(1))
                current_h = int(size_match.group(2))
                fps_list = []
                continue

            fps_match = re.match(r"\s+Interval:.*\((\d+\.?\d*)\s+fps\)", line)
            if fps_match:
                fps_list.append(float(fps_match.group(1)))

        # Flush last
        if current_fmt and current_w and fps_list:
            formats.append(VideoFormat(
                width=current_w, height=current_h,
                fps=list(fps_list), pixel_format=current_fmt,
                description=current_desc,
            ))
        return formats

    # -- controls ------------------------------------------------------------

    def get_controls(self, camera: CameraInfo) -> list[CameraControl]:
        controls: list[CameraControl] = []
        try:
            result = subprocess.run(
                ["v4l2-ctl", "-d", camera.device_path, "--list-ctrls-menus"],
                capture_output=True,
                text=True,
            )
            controls = self._parse_controls(result.stdout)
        except Exception:
            pass
        return controls

    def _parse_controls(self, output: str) -> list[CameraControl]:
        controls: list[CameraControl] = []
        menu_items: dict[str, list[str]] = {}
        last_ctrl_id = ""

        for line in output.splitlines():
            # Control line: "brightness 0x00980900 (int)    : min=0 max=255 step=1 default=128 value=128"
            ctrl_match = re.match(
                r"\s*(\w+)\s+0x[0-9a-f]+\s+\((\w+)\)\s*:\s*(.*)", line
            )
            if ctrl_match:
                ctrl_id = ctrl_match.group(1)
                ctrl_type_str = ctrl_match.group(2)
                params_str = ctrl_match.group(3)
                last_ctrl_id = ctrl_id

                params = self._parse_ctrl_params(params_str)
                category = _CATEGORY_MAP.get(ctrl_id, ControlCategory.ADVANCED)
                label = _CONTROL_LABELS.get(ctrl_id, ctrl_id.replace("_", " ").title())

                if ctrl_type_str == "int":
                    ctype = ControlType.INTEGER
                elif ctrl_type_str == "bool":
                    ctype = ControlType.BOOLEAN
                elif ctrl_type_str == "menu":
                    ctype = ControlType.MENU
                elif ctrl_type_str == "button":
                    ctype = ControlType.BUTTON
                else:
                    ctype = ControlType.INTEGER

                ctrl = CameraControl(
                    id=ctrl_id,
                    name=label,
                    category=category,
                    control_type=ctype,
                    value=params.get("value", 0),
                    default=params.get("default", 0),
                    minimum=params.get("min"),
                    maximum=params.get("max"),
                    step=params.get("step", 1),
                    flags=params.get("flags", ""),
                )
                controls.append(ctrl)
                continue

            # Menu entry:  "                1: Manual Mode"
            menu_match = re.match(r"\s+(\d+):\s+(.+)", line)
            if menu_match and last_ctrl_id:
                menu_items.setdefault(last_ctrl_id, []).append(menu_match.group(2).strip())

        # Attach menu choices
        for ctrl in controls:
            if ctrl.control_type == ControlType.MENU and ctrl.id in menu_items:
                ctrl.choices = menu_items[ctrl.id]

        return controls

    @staticmethod
    def _parse_ctrl_params(params_str: str) -> dict[str, Any]:
        params: dict[str, Any] = {}
        for token in re.findall(r"(\w+)=(-?\d+)", params_str):
            params[token[0]] = int(token[1])
        flags_match = re.search(r"flags=(\w+)", params_str)
        if flags_match:
            params["flags"] = flags_match.group(1)
        return params

    def set_control(self, camera: CameraInfo, control_id: str, value: Any) -> bool:
        try:
            subprocess.run(
                ["v4l2-ctl", "-d", camera.device_path, "--set-ctrl", f"{control_id}={value}"],
                capture_output=True,
                check=True,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    # -- gstreamer -----------------------------------------------------------

    def get_gst_source(self, camera: CameraInfo, fmt: VideoFormat | None = None) -> str:
        device = camera.device_path

        # Try PipeWire first — allows sharing the camera with other apps
        pw_node_id = self._find_pw_node_id(device)
        if pw_node_id is not None:
            return self._pw_gst_source(pw_node_id, camera, fmt)

        # Fallback: direct V4L2 (exclusive access)
        return self._v4l2_gst_source(device, camera, fmt)

    def _pw_gst_source(self, node_id: int, camera: CameraInfo, fmt: VideoFormat | None) -> str:
        """Build pipewiresrc element — PipeWire allows multi-app camera sharing."""
        src = f"pipewiresrc target-object={node_id}"
        if fmt is None:
            fmt = self._pick_best_format(camera)
        if fmt:
            if fmt.pixel_format == "MJPG":
                caps = f"image/jpeg,width={fmt.width},height={fmt.height}"
                if fmt.fps:
                    best_fps = int(max(fmt.fps))
                    caps += f",framerate={best_fps}/1"
                return f"{src} ! {caps} ! jpegdec"
            caps = f"video/x-raw,width={fmt.width},height={fmt.height}"
            if fmt.fps:
                best_fps = int(max(fmt.fps))
                caps += f",framerate={best_fps}/1"
            return f"{src} ! {caps}"
        return src

    def _v4l2_gst_source(self, device: str, camera: CameraInfo, fmt: VideoFormat | None) -> str:
        """Build v4l2src element — exclusive device access."""
        src = f"v4l2src device={device}"
        if fmt is None:
            fmt = self._pick_best_format(camera)
        if fmt:
            if fmt.pixel_format == "MJPG":
                caps = f"image/jpeg,width={fmt.width},height={fmt.height}"
                if fmt.fps:
                    best_fps = int(max(fmt.fps))
                    caps += f",framerate={best_fps}/1"
                return f"{src} ! {caps} ! jpegdec"
            caps = f"video/x-raw,width={fmt.width},height={fmt.height}"
            if fmt.fps:
                best_fps = int(max(fmt.fps))
                caps += f",framerate={best_fps}/1"
            return f"{src} ! {caps}"
        return src

    @staticmethod
    def _find_pw_node_id(device_path: str) -> int | None:
        """Find PipeWire node ID for a V4L2 device path (e.g. /dev/video0).

        Returns the numeric node ID or None if PipeWire is unavailable.
        """
        try:
            result = subprocess.run(
                ["pw-dump"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None
            data = json.loads(result.stdout)
            for obj in data:
                props = obj.get("info", {}).get("props", {})
                if (
                    props.get("media.class") == "Video/Source"
                    and props.get("api.v4l2.path") == device_path
                ):
                    node_id = obj.get("id")
                    if node_id is not None:
                        log.info(
                            "PipeWire node %d found for %s", node_id, device_path
                        )
                        return int(node_id)
        except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass
        return None

    def _pick_best_format(self, camera: CameraInfo) -> VideoFormat | None:
        """Auto-select format: prefer MJPEG at highest resolution with 30fps."""
        if not camera.formats:
            return None
        mjpeg = [f for f in camera.formats if f.pixel_format == "MJPG" and f.fps and max(f.fps) >= 25]
        raw = [f for f in camera.formats if f.pixel_format != "MJPG" and f.fps and max(f.fps) >= 25]
        # Prefer MJPEG for higher resolutions (lower USB bandwidth)
        candidates = mjpeg if mjpeg else raw
        if not candidates:
            candidates = camera.formats
        candidates.sort(key=lambda f: (f.width * f.height, max(f.fps) if f.fps else 0), reverse=True)
        return candidates[0]

    # -- photo ---------------------------------------------------------------

    def can_capture_photo(self) -> bool:
        return True

    def capture_photo(self, camera: CameraInfo, output_path: str) -> bool:
        """Capture a single JPEG frame via ffmpeg (one-shot)."""
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-f", "v4l2",
                    "-i", camera.device_path,
                    "-frames:v", "1",
                    "-q:v", "2",
                    output_path,
                ],
                capture_output=True,
                check=True,
                timeout=10,
            )
            return os.path.isfile(output_path)
        except Exception:
            return False
