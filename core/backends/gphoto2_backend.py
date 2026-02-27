"""GPhoto2 backend – covers 2 500+ DSLR and mirrorless cameras."""

from __future__ import annotations

import fcntl
import logging
import os
import re
import subprocess
import time
from typing import Any

from constants import BackendType, ControlCategory, ControlType, BASE_DIR
from core.camera_backend import CameraBackend, CameraControl, CameraInfo, VideoFormat
from utils.i18n import _

log = logging.getLogger(__name__)

# Unique UDP port per process instance (avoids conflicts with multi-instance)
_UDP_PORT = 5000 + (os.getpid() % 1000)

# ioctl constant for USB device reset
_USBDEVFS_RESET = 21780


class GPhoto2Backend(CameraBackend):
    """Backend for DSLR / mirrorless cameras via libgphoto2."""

    _streaming_process: subprocess.Popen | None = None

    def get_backend_type(self) -> BackendType:
        return BackendType.GPHOTO2

    @staticmethod
    def _usb_reset_canon() -> bool:
        """Reset Canon camera USB to clear PTP timeout state."""
        try:
            result = subprocess.run(["lsusb"], capture_output=True, text=True)
            for line in result.stdout.strip().split("\n"):
                if "04a9" in line:  # Canon vendor ID
                    parts = line.split()
                    bus = parts[1]
                    dev = parts[3].rstrip(":")
                    path = f"/dev/bus/usb/{bus}/{dev}"
                    fd = os.open(path, os.O_WRONLY)
                    fcntl.ioctl(fd, _USBDEVFS_RESET, 0)
                    os.close(fd)
                    log.info("USB reset OK: %s", path)
                    time.sleep(2)
                    return True
        except Exception as exc:
            log.warning("USB reset failed: %s", exc)
        return False

    def is_available(self) -> bool:
        try:
            subprocess.run(["gphoto2", "--version"], capture_output=True, check=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            return False

    # -- detection -----------------------------------------------------------

    _streaming_active = False

    def detect_cameras(self) -> list[CameraInfo]:
        # Skip detection while streaming is active — gphoto2 can't share USB
        if self._streaming_active:
            return []

        cameras: list[CameraInfo] = []
        try:
            # Kill GVFS to release the camera
            subprocess.run(
                ["pkill", "-f", "gvfs-gphoto2-volume-monitor"],
                capture_output=True,
            )
            time.sleep(0.5)

            result = subprocess.run(
                ["gphoto2", "--auto-detect"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return cameras

            for line in result.stdout.strip().splitlines()[2:]:
                line = line.strip()
                if not line or "usb:" not in line:
                    continue
                parts = line.split("usb:")
                if len(parts) < 2:
                    continue
                name = parts[0].strip() or _("Generic Camera")
                port = "usb:" + parts[1].strip()
                cam = CameraInfo(
                    id=f"gphoto2:{port}",
                    name=name,
                    backend=BackendType.GPHOTO2,
                    device_path=port,
                    capabilities=["photo", "video"],
                    extra={"port": port, "udp_port": _UDP_PORT},
                )
                cameras.append(cam)
        except Exception:
            pass
        return cameras

    # -- controls ------------------------------------------------------------

    def get_controls(self, camera: CameraInfo) -> list[CameraControl]:
        controls: list[CameraControl] = []
        port = camera.extra.get("port", camera.device_path)
        try:
            result = subprocess.run(
                ["gphoto2", "--port", port, "--list-all-config"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                return controls
            config_paths = [l.strip() for l in result.stdout.splitlines() if l.strip().startswith("/")]
            for cfg_path in config_paths:
                ctrl = self._read_config(port, cfg_path)
                if ctrl:
                    controls.append(ctrl)
        except Exception:
            pass
        return controls

    def _read_config(self, port: str, cfg_path: str) -> CameraControl | None:
        try:
            result = subprocess.run(
                ["gphoto2", "--port", port, "--get-config", cfg_path],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return None
            return self._parse_config(cfg_path, result.stdout)
        except Exception:
            return None

    @staticmethod
    def _parse_config(cfg_path: str, output: str) -> CameraControl | None:
        lines = output.strip().splitlines()
        info: dict[str, str] = {}
        choices: list[str] = []
        for line in lines:
            if line.startswith("Label:"):
                info["label"] = line.split(":", 1)[1].strip()
            elif line.startswith("Type:"):
                info["type"] = line.split(":", 1)[1].strip()
            elif line.startswith("Current:"):
                info["current"] = line.split(":", 1)[1].strip()
            elif line.startswith("Choice:"):
                # "Choice: 0 Auto"
                parts = line.split(" ", 2)
                if len(parts) >= 3:
                    choices.append(parts[2].strip())
            elif line.startswith("Bottom:"):
                info["min"] = line.split(":", 1)[1].strip()
            elif line.startswith("Top:"):
                info["max"] = line.split(":", 1)[1].strip()
            elif line.startswith("Step:"):
                info["step"] = line.split(":", 1)[1].strip()
            elif line.startswith("Readonly:"):
                info["readonly"] = line.split(":", 1)[1].strip()

        if "label" not in info:
            return None

        # Determine control type
        gp_type = info.get("type", "TEXT")
        if gp_type == "RADIO" or gp_type == "MENU":
            ctype = ControlType.MENU
        elif gp_type == "TOGGLE":
            ctype = ControlType.BOOLEAN
        elif gp_type == "RANGE":
            ctype = ControlType.INTEGER
        else:
            return None  # Skip TEXT and DATE types for UI

        # Determine category by config path
        cat = ControlCategory.ADVANCED
        path_lower = cfg_path.lower()
        if "iso" in path_lower or "exposure" in path_lower or "shutterspeed" in path_lower:
            cat = ControlCategory.EXPOSURE
        elif "whitebalance" in path_lower or "wb" in path_lower:
            cat = ControlCategory.WHITE_BALANCE
        elif "focus" in path_lower:
            cat = ControlCategory.FOCUS
        elif "contrast" in path_lower or "saturation" in path_lower or "sharpness" in path_lower:
            cat = ControlCategory.IMAGE

        current = info.get("current", "0")
        flags = "read-only" if info.get("readonly", "0") == "1" else ""

        ctrl = CameraControl(
            id=cfg_path,
            name=info["label"],
            category=cat,
            control_type=ctype,
            value=current,
            default=current,
            flags=flags,
        )

        if ctype == ControlType.INTEGER:
            try:
                ctrl.minimum = int(info.get("min", 0))
                ctrl.maximum = int(info.get("max", 100))
                ctrl.step = int(info.get("step", 1))
                ctrl.value = int(current)
                ctrl.default = int(current)
            except ValueError:
                pass
        elif ctype == ControlType.MENU and choices:
            ctrl.choices = choices
        elif ctype == ControlType.BOOLEAN:
            ctrl.value = current.lower() in ("1", "true", "on")
            ctrl.default = ctrl.value

        return ctrl

    def set_control(self, camera: CameraInfo, control_id: str, value: Any) -> bool:
        port = camera.extra.get("port", camera.device_path)
        try:
            subprocess.run(
                ["gphoto2", "--port", port, "--set-config", f"{control_id}={value}"],
                capture_output=True,
                check=True,
                timeout=10,
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    # -- gstreamer -----------------------------------------------------------

    def get_gst_source(self, camera: CameraInfo, fmt: VideoFormat | None = None) -> str:
        udp_port = camera.extra.get("udp_port", 5000)
        return (
            f'udpsrc port={udp_port} address=127.0.0.1 '
            f'caps="video/mpegts,packetsize=(int)1316" ! '
            f"queue max-size-bytes=2097152 ! tsdemux ! decodebin ! videoconvert"
        )

    def start_streaming(self, camera: CameraInfo) -> bool:
        """Launch the gphoto2 streaming script — identical to old working app.

        Uses subprocess.run() (blocking) exactly as the old app does.
        The script itself handles all cleanup (kill old processes, GVFS, etc).
        """
        self._streaming_active = True
        port = camera.extra.get("port", camera.device_path)
        udp_port = str(camera.extra.get("udp_port", 5000))
        script = os.path.join(BASE_DIR, "script", "run_webcam_gphoto2.sh")
        if not os.path.isfile(script):
            script = os.path.join(BASE_DIR, "script", "run_webcam.sh")
        if not os.path.isfile(script):
            log.error("GPhoto2 streaming script not found: %s", script)
            return False

        if not os.access(script, os.X_OK):
            try:
                os.chmod(script, 0o755)
            except OSError:
                pass

        port_arg = port if port else ""
        try:
            import tempfile
            with tempfile.TemporaryFile("w+") as f:
                res = subprocess.run(
                    [script, port_arg, udp_port, camera.name],
                    stdout=f,
                    stderr=subprocess.STDOUT,
                )
                f.seek(0)
                output = f.read().strip()
            log.info("gphoto2 script output:\n%s", output)

            if res.returncode == 0:
                for line in output.split("\n"):
                    if line.startswith("SUCCESS:"):
                        dev = line.split("SUCCESS:")[1].strip()
                        log.info("GPhoto2 streaming started on %s", dev)
                        return True
                log.info("GPhoto2 script exited 0 (no explicit SUCCESS)")
                return True

            log.error("GPhoto2 script failed (code %d): %s",
                      res.returncode, output)
            self._streaming_active = False
            return False
        except Exception as exc:
            log.error("Failed to start gphoto2 streaming: %s", exc)
            self._streaming_active = False
            return False

    def stop_streaming(self) -> None:
        """Stop ALL gphoto2/ffmpeg processes and wait for USB release."""
        self._streaming_active = False
        try:
            subprocess.run(["pkill", "-9", "-f", "gphoto2 --"],
                           capture_output=True)
            subprocess.run(["pkill", "-9", "-f", "ffmpeg.*mpegts"],
                           capture_output=True)
            subprocess.run(["pkill", "-9", "-f", "ffmpeg.*v4l2"],
                           capture_output=True)
        except Exception:
            pass
        self._streaming_process = None
        time.sleep(2)

    def needs_streaming_setup(self) -> bool:
        """GPhoto2 requires an external streaming process."""
        return True

    # -- photo ---------------------------------------------------------------

    def can_capture_photo(self) -> bool:
        return True

    def capture_photo(self, camera: CameraInfo, output_path: str) -> bool:
        port = camera.extra.get("port", camera.device_path)
        try:
            # Kill gvfs interference
            subprocess.run(
                ["pkill", "-f", "gvfs-gphoto2-volume-monitor"],
                capture_output=True,
            )
            time.sleep(0.5)

            camera_arg = ["--port", port] if port else []
            subprocess.run(
                [
                    "gphoto2", *camera_arg,
                    "--capture-image-and-download",
                    "--filename", output_path,
                    "--force-overwrite",
                    "--keep",
                ],
                capture_output=True,
                check=True,
                timeout=60,
            )
            return os.path.isfile(output_path)
        except Exception:
            return False


