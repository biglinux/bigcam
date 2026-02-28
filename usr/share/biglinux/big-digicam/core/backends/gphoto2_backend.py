"""GPhoto2 backend – covers 2 500+ DSLR and mirrorless cameras."""

from __future__ import annotations

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


class GPhoto2Backend(CameraBackend):
    """Backend for DSLR / mirrorless cameras via libgphoto2."""

    _streaming_process: subprocess.Popen | None = None

    def get_backend_type(self) -> BackendType:
        return BackendType.GPHOTO2

    def is_available(self) -> bool:
        try:
            subprocess.run(["gphoto2", "--version"], capture_output=True, check=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            return False

    # -- detection -----------------------------------------------------------

    def detect_cameras(self) -> list[CameraInfo]:
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

    # Keyword-to-category mapping for individual config names
    _CONTROL_CATEGORY: dict[str, ControlCategory] = {
        # Exposure
        "iso": ControlCategory.EXPOSURE,
        "shutterspeed": ControlCategory.EXPOSURE,
        "aperture": ControlCategory.EXPOSURE,
        "f-number": ControlCategory.EXPOSURE,
        "exposurecompensation": ControlCategory.EXPOSURE,
        "autoexposuremode": ControlCategory.EXPOSURE,
        "autoexposuremodedial": ControlCategory.EXPOSURE,
        "expprogram": ControlCategory.EXPOSURE,
        "meteringmode": ControlCategory.EXPOSURE,
        "aeb": ControlCategory.EXPOSURE,
        "bracketmode": ControlCategory.EXPOSURE,
        "exposuremetermode": ControlCategory.EXPOSURE,
        "exposureiso": ControlCategory.EXPOSURE,
        "aebracket": ControlCategory.EXPOSURE,
        "manualexposurecompensation": ControlCategory.EXPOSURE,
        # Flash (under exposure)
        "flashmode": ControlCategory.EXPOSURE,
        "flashcompensation": ControlCategory.EXPOSURE,
        "internalflashmode": ControlCategory.EXPOSURE,
        "flashopen": ControlCategory.EXPOSURE,
        "flashcharge": ControlCategory.EXPOSURE,
        # Focus
        "focusmode": ControlCategory.FOCUS,
        "manualfocusdrive": ControlCategory.FOCUS,
        "autofocusdrive": ControlCategory.FOCUS,
        "focusarea": ControlCategory.FOCUS,
        "focuspoints": ControlCategory.FOCUS,
        "continuousaf": ControlCategory.FOCUS,
        "cancelautofocus": ControlCategory.FOCUS,
        "afbeam": ControlCategory.FOCUS,
        "afmethod": ControlCategory.FOCUS,
        "focuslock": ControlCategory.FOCUS,
        "afoperation": ControlCategory.FOCUS,
        # White balance
        "whitebalance": ControlCategory.WHITE_BALANCE,
        "whitebalanceadjust": ControlCategory.WHITE_BALANCE,
        "whitebalanceadjusta": ControlCategory.WHITE_BALANCE,
        "whitebalancexa": ControlCategory.WHITE_BALANCE,
        "whitebalancexb": ControlCategory.WHITE_BALANCE,
        "colortemperature": ControlCategory.WHITE_BALANCE,
        "wb_adjust": ControlCategory.WHITE_BALANCE,
        # Image quality / processing
        "imageformat": ControlCategory.IMAGE,
        "imageformatsd": ControlCategory.IMAGE,
        "imageformatcf": ControlCategory.IMAGE,
        "imageformatexthd": ControlCategory.IMAGE,
        "imagesize": ControlCategory.IMAGE,
        "imagequality": ControlCategory.IMAGE,
        "picturestyle": ControlCategory.IMAGE,
        "colorspace": ControlCategory.IMAGE,
        "contrast": ControlCategory.IMAGE,
        "saturation": ControlCategory.IMAGE,
        "sharpness": ControlCategory.IMAGE,
        "hue": ControlCategory.IMAGE,
        "colormodel": ControlCategory.IMAGE,
        "highlighttonepr": ControlCategory.IMAGE,
        "shadowtonepr": ControlCategory.IMAGE,
        "highisonr": ControlCategory.IMAGE,
        "longexpnr": ControlCategory.IMAGE,
        "aspectratio": ControlCategory.IMAGE,
        # Capture settings
        "drivemode": ControlCategory.CAPTURE,
        "capturemode": ControlCategory.CAPTURE,
        "capturetarget": ControlCategory.CAPTURE,
        "eosremoterelease": ControlCategory.CAPTURE,
        "viewfinder": ControlCategory.CAPTURE,
        "reviewtime": ControlCategory.CAPTURE,
        "eoszoomposition": ControlCategory.CAPTURE,
        "eoszoom": ControlCategory.CAPTURE,
        "eosvfmode": ControlCategory.CAPTURE,
        "output": ControlCategory.CAPTURE,
        "movieservoaf": ControlCategory.CAPTURE,
        "liveviewsize": ControlCategory.CAPTURE,
        "remotemode": ControlCategory.CAPTURE,
        # Status (read-only info)
        "batterylevel": ControlCategory.STATUS,
        "lensname": ControlCategory.STATUS,
        "serialnumber": ControlCategory.STATUS,
        "cameramodel": ControlCategory.STATUS,
        "deviceversion": ControlCategory.STATUS,
        "availableshots": ControlCategory.STATUS,
        "eosserialnumber": ControlCategory.STATUS,
        "firmwareversion": ControlCategory.STATUS,
        "model": ControlCategory.STATUS,
        "ptpversion": ControlCategory.STATUS,
    }

    # Broader fallback: map by gPhoto2 config section
    _SECTION_CATEGORY: dict[str, ControlCategory] = {
        "imgsettings": ControlCategory.IMAGE,
        "capturesettings": ControlCategory.CAPTURE,
        "status": ControlCategory.STATUS,
        "settings": ControlCategory.ADVANCED,
        "actions": ControlCategory.ADVANCED,
        "other": ControlCategory.ADVANCED,
    }

    _BATCH_SIZE = 50

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
            config_paths = [
                line.strip()
                for line in result.stdout.splitlines()
                if line.strip().startswith("/")
            ]
            if not config_paths:
                return controls

            # Batch-read configs to avoid one subprocess per control
            for start in range(0, len(config_paths), self._BATCH_SIZE):
                batch = config_paths[start : start + self._BATCH_SIZE]
                cmd = ["gphoto2", "--port", port]
                for cfg in batch:
                    cmd.extend(["--get-config", cfg])
                res = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=30,
                )
                if res.returncode != 0:
                    # Fallback: try one-by-one for this batch
                    for cfg in batch:
                        ctrl = self._read_single_config(port, cfg)
                        if ctrl:
                            controls.append(ctrl)
                    continue
                controls.extend(self._parse_batch_output(batch, res.stdout))
        except Exception as exc:
            log.warning("get_controls failed: %s", exc)
        return controls

    def _read_single_config(self, port: str, cfg_path: str) -> CameraControl | None:
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

    @classmethod
    def _parse_batch_output(
        cls, paths: list[str], output: str,
    ) -> list[CameraControl]:
        """Split combined gphoto2 output into per-config blocks and parse."""
        controls: list[CameraControl] = []
        blocks: list[list[str]] = []
        current: list[str] = []
        for line in output.splitlines():
            if line.startswith("Label:") and current:
                blocks.append(current)
                current = []
            current.append(line)
        if current:
            blocks.append(current)

        for idx, block in enumerate(blocks):
            if idx >= len(paths):
                break
            ctrl = cls._parse_config(paths[idx], "\n".join(block))
            if ctrl:
                controls.append(ctrl)
        return controls

    @classmethod
    def _categorize(cls, cfg_path: str) -> ControlCategory:
        """Map a gPhoto2 config path to a ControlCategory."""
        parts = cfg_path.strip("/").lower().split("/")
        # Check leaf name first (most specific)
        leaf = parts[-1] if parts else ""
        if leaf in cls._CONTROL_CATEGORY:
            return cls._CONTROL_CATEGORY[leaf]
        # Check section (e.g. /main/capturesettings/...)
        for part in parts:
            if part in cls._SECTION_CATEGORY:
                return cls._SECTION_CATEGORY[part]
        return ControlCategory.ADVANCED

    @classmethod
    def _parse_config(cls, cfg_path: str, output: str) -> CameraControl | None:
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

        gp_type = info.get("type", "TEXT")
        if gp_type in ("RADIO", "MENU"):
            ctype = ControlType.MENU
        elif gp_type == "TOGGLE":
            ctype = ControlType.BOOLEAN
        elif gp_type == "RANGE":
            ctype = ControlType.INTEGER
        elif gp_type == "TEXT":
            ctype = ControlType.STRING
        elif gp_type == "DATE":
            ctype = ControlType.STRING
        else:
            return None

        cat = cls._categorize(cfg_path)
        current = info.get("current", "")
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
            res = subprocess.run(
                [script, port_arg, udp_port],
                capture_output=True,
                text=True,
            )
            output = res.stdout.strip()
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
            return False
        except Exception as exc:
            log.error("Failed to start gphoto2 streaming: %s", exc)
            return False

    def stop_streaming(self) -> None:
        """Stop ALL gphoto2/ffmpeg processes and wait for USB release."""
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


