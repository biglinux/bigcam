"""GPhoto2 backend – covers 2 500+ DSLR and mirrorless cameras."""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
from utils.command_runner import SecureCommandRunner
import threading
import time
from typing import Any

from constants import BackendType, ControlCategory, ControlType, BASE_DIR
from core.camera_backend import CameraBackend, CameraControl, CameraInfo, VideoFormat
from utils.i18n import _
from core.gphoto_session import GPhotoSession

log = logging.getLogger(__name__)

# Unique UDP port per process instance (avoids conflicts with multi-instance)
_UDP_PORT = 5000 + (os.getpid() % 1000)


class GPhoto2Backend(CameraBackend):
    """Backend for DSLR / mirrorless cameras via libgphoto2."""

    _streaming_process: subprocess.Popen | None = None
    # Track active streaming sessions per camera port
    # port -> {"udp_port": str, "launch_port": str}
    _active_streams: dict[str, dict[str, str]] = {}
    _streams_lock = threading.Lock()
    _streaming_active: bool = False
    _last_detected: list[CameraInfo] = []

    def get_backend_type(self) -> BackendType:
        return BackendType.GPHOTO2





    @staticmethod
    def _diagnose_usb(port: str) -> None:
        """Print diagnostic info about a USB device for debugging."""
        try:
            bus, dev = port.replace("usb:", "").split(",")
            usb_path = f"/dev/bus/usb/{bus}/{dev}"

            # Check device existence and permissions
            exists = os.path.exists(usb_path)
            log.debug(f"USB diag: {usb_path} exists={exists}")
            if not exists:
                # Show what devices ARE on this bus
                bus_dir = f"/dev/bus/usb/{bus}"
                if os.path.isdir(bus_dir):
                    devs = sorted(os.listdir(bus_dir))
                    log.debug(f"USB diag: devices on bus {bus}: {devs}")
                return

            # Check file permissions
            import stat

            st = os.stat(usb_path)
            mode = stat.filemode(st.st_mode)
            log.debug(
                f"USB diag: {usb_path} mode={mode} uid={st.st_uid} gid={st.st_gid}"
            )

            # Check lsusb for this specific device
            result = SecureCommandRunner.run_safe(
                ["lsusb", "-s", f"{bus}:{dev}"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            log.debug(f"USB diag lsusb: {result.stdout.strip()}")

            # Check fuser
            result = SecureCommandRunner.run_safe(
                ["fuser", usb_path],
                capture_output=True,
                text=True,
                timeout=5,
            )
            holders = result.stdout.strip()
            log.debug(f"USB diag fuser: '{holders}'")

            # Check gphoto2 --auto-detect
            result = SecureCommandRunner.run_safe(
                ["gphoto2", "--auto-detect"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            lines = [
                ln.strip()
                for ln in result.stdout.strip().splitlines()[2:]
                if ln.strip()
            ]
            log.debug(f"USB diag auto-detect: {lines}")

            # Check dmesg for recent USB errors on this bus
            result = SecureCommandRunner.run_safe(
                ["dmesg", "--time-format=reltime"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            usb_errors = [
                ln
                for ln in result.stdout.splitlines()[-50:]
                if f"usb {bus.lstrip('0') or '0'}-" in ln.lower()
                or "error" in ln.lower()
                and "usb" in ln.lower()
            ]
            if usb_errors:
                log.debug(f"USB diag dmesg errors: {usb_errors[-5:]}")
        except Exception as exc:
            log.debug(f"USB diag error: {exc}")

    def is_available(self) -> bool:
        try:
            SecureCommandRunner.run_safe(["gphoto2", "--version"], capture_output=True, check=True, timeout=5)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def _check_capture_support(port: str) -> bool:
        """Return True if the camera at *port* supports capture operations."""
        env = {**os.environ, "LANG": "C", "LC_ALL": "C"}
        # Try without --port first (doesn't need device access, works even
        # when GVFS still holds the device), then fall back to --port.
        for cmd in (
            ["gphoto2", "--abilities"],
            ["gphoto2", "--port", port, "--abilities"],
        ):
            try:
                result = SecureCommandRunner.run_safe(
                    cmd, capture_output=True, text=True, timeout=15, env=env,
                )
                if result.returncode != 0:
                    continue
                out = result.stdout.lower()
                if "not supported" in out:
                    return False
                return True
            except Exception:
                continue
        return True  # assume supported if all checks fail

    @staticmethod
    def _has_remote_control(port: str) -> bool:
        """Return True if the camera exposes capture/image settings via PTP.

        Cameras in basic MTP/PTP mode (file-transfer only) expose only
        status entries.  Remote-controllable cameras also expose
        capturesettings and/or imgsettings.
        """
        env = {**os.environ, "LANG": "C", "LC_ALL": "C"}
        try:
            result = SecureCommandRunner.run_safe(
                ["gphoto2", "--port", port, "--list-config"],
                capture_output=True, text=True, timeout=15, env=env,
            )
            if result.returncode != 0:
                return True  # assume OK if we can't check
            lines = result.stdout.strip().splitlines()
            # A camera with real remote control has capturesettings or imgsettings
            for line in lines:
                if "/capturesettings/" in line or "/imgsettings/" in line:
                    return True
            # Very few config entries = basic PTP, no remote control
            if len(lines) <= 15:
                log.info(
                    "Camera at %s has only %d config entries and no "
                    "capturesettings — likely MTP/basic PTP only",
                    port, len(lines),
                )
                return False
            return True
        except Exception:
            return True  # assume OK on error

    # -- detection -----------------------------------------------------------

    def detect_cameras(self) -> list[CameraInfo]:
        cameras: list[CameraInfo] = []
        try:
            # Retry up to 2 times in case GVFS hasn't released the device yet
            max_attempts = 1 if self._streaming_active else 2
            for attempt in range(max_attempts):
                result = SecureCommandRunner.run_safe(
                    ["gphoto2", "--auto-detect"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if result.returncode != 0:
                    break

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
                        extra={"port": port, "udp_port": _UDP_PORT + len(cameras)},
                    )
                    cameras.append(cam)

                if cameras:
                    break
                if not self._streaming_active:
                    time.sleep(0.3)
        except Exception:
            pass
        if cameras:
            self._last_detected = cameras
        elif self._streaming_active:
            # Fallback: if --auto-detect failed during streaming, keep previous
            # BUT verify the USB device still physically exists first
            still_connected = False
            for cam in self._last_detected:
                port = cam.extra.get("port", cam.device_path)
                m = re.match(r"usb:(\d+),(\d+)", port)
                if m:
                    usb_path = f"/dev/bus/usb/{m.group(1)}/{m.group(2)}"
                    if os.path.exists(usb_path):
                        still_connected = True
                        break
            if still_connected:
                return self._last_detected
            # USB device gone — camera was physically disconnected
            log.info("gphoto2 camera USB device removed, clearing streaming state")
            self._streaming_active = False
            self._last_detected = []
        return cameras

    # -- controls ------------------------------------------------------------

    @classmethod
    def _refresh_port(cls, camera: CameraInfo) -> str:
        # A model label is not a device identifier. A disconnected camera must be
        # rediscovered, not silently replaced by a second camera with the same name.
        return camera.extra.get("port", camera.device_path)

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

        # Refresh USB port first (device number changes after GVFS kill)
        port = self._refresh_port(camera)
        log.debug(f"get_controls: port={port}")

        # Check if the USB device actually exists
        try:
            bus, dev = port.replace("usb:", "").split(",")
            usb_path = f"/dev/bus/usb/{bus}/{dev}"
            if not os.path.exists(usb_path):
                log.debug(
                    f"get_controls: {usb_path} does not exist, camera disconnected?"
                )
                return controls
        except (ValueError, OSError):
            pass

        # Ensure GVFS is dead and USB device is free
        # Other applications retain ownership of their USB sessions.
        # Other applications retain ownership of their USB sessions.
        # Diagnostic: check USB device accessibility
        self._diagnose_usb(port)

        delays = [0, 3, 5]
        try:
            for attempt, delay in enumerate(delays, 1):
                if delay:
                    log.debug(f"get_controls: waiting {delay}s before retry...")
                    time.sleep(delay)
                    # Other applications retain ownership of their USB sessions.
                    # Other applications retain ownership of their USB sessions.
                    # Re-diagnose after wait
                    self._diagnose_usb(port)

                log.debug(f"get_controls attempt {attempt}/{len(delays)}")
                result = SecureCommandRunner.run_safe(
                    ["gphoto2", "--port", port, "--list-all-config"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                stdout_preview = result.stdout[:300] if result.returncode != 0 else ""
                log.debug(
                    f"--list-all-config rc={result.returncode}, "
                    f"stdout_lines={len(result.stdout.splitlines())}, "
                    f"stderr={result.stderr.strip()[:200]}"
                )
                if result.returncode != 0 and stdout_preview:
                    log.debug(f"stdout preview: {stdout_preview}")
                if result.returncode == 0 and result.stdout.strip():
                    break
            else:
                # Last resort: re-detect port and try once more
                port = self._refresh_port(camera)
                log.debug(f"get_controls fallback port={port}")
                # Other applications retain ownership of their USB sessions.
                self._diagnose_usb(port)
                result = SecureCommandRunner.run_safe(
                    ["gphoto2", "--port", port, "--list-all-config"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if result.returncode != 0 or not result.stdout.strip():
                    log.debug("get_controls: all attempts failed")
                    return controls

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
                res = SecureCommandRunner.run_safe(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
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
            result = SecureCommandRunner.run_safe(
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
        cls,
        paths: list[str],
        output: str,
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
            SecureCommandRunner.run_safe(
                ["gphoto2", "--port", port, "--set-config", f"{control_id}={value}"],
                capture_output=True,
                check=True,
                timeout=10,
            )
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            return False

    # -- gstreamer -----------------------------------------------------------

    def get_gst_source(self, camera: CameraInfo, fmt: VideoFormat | None = None) -> str:
        udp_port = camera.extra.get("udp_port", 5000)
        return (
            f"udpsrc port={udp_port} address=127.0.0.1 "
            f'caps="video/mpegts,packetsize=(int)1316" ! '
            f"queue max-size-bytes=2097152 leaky=downstream ! tsdemux ! decodebin ! videoconvert"
        )

    def start_streaming(self, camera: CameraInfo) -> bool:
        port = self._refresh_port(camera)
        with self._streams_lock:
            previous = self._active_streams.get(port)
            if previous and previous["session"].running:
                return True
        self.stop_streaming(camera)
        if not self._check_capture_support(port):
            camera.extra["capture_unsupported"] = True
            return False
        if not self._has_remote_control(port):
            camera.extra["ptp_streaming_error"] = True
            return False
        try:
            session = GPhotoSession(port, int(camera.extra.get("udp_port", 5000)))
            with self._streams_lock:
                # Serialize producer startup for this backend; no detached shell.
                previous = self._active_streams.get(port)
                if previous and previous["session"].running:
                    return True
                if not session.start():
                    session.stop()
                    return False
                self._active_streams[port] = {"session": session, "launch_port": port,
                                             "udp_port": str(session.udp_port), "vcam_device": "none"}
                self._streaming_active = True
            return True
        except (OSError, ValueError, subprocess.SubprocessError):
            log.exception("Could not start the selected DSLR producer")
            return False

    def stop_streaming(self, camera: CameraInfo | None = None) -> None:
        """Stop only Popen objects created by this backend instance/session."""
        with self._streams_lock:
            if camera is None:
                sessions = list(self._active_streams.values())
                self._active_streams.clear()
            else:
                entry = self._active_streams.pop(camera.extra.get("port", camera.device_path), None)
                sessions = [entry] if entry else []
            self._streaming_active = bool(self._active_streams)
        for entry in sessions:
            try:
                entry["session"].stop()
            except (OSError, subprocess.SubprocessError):
                log.exception("Could not stop an owned DSLR producer")

    def needs_streaming_setup(self) -> bool:
        """GPhoto2 requires an external streaming process."""
        return True

    def is_camera_streaming(self, camera: CameraInfo) -> bool:
        port = camera.extra.get("port", camera.device_path)
        with self._streams_lock:
            entry = self._active_streams.get(port)
            return bool(entry and entry["session"].running)

    # -- photo ---------------------------------------------------------------

    def can_capture_photo(self) -> bool:
        return True

    def capture_photo(self, camera: CameraInfo, output_path: str) -> bool:
        port = camera.extra.get("port", camera.device_path)
        if not re.fullmatch(r"usb:[0-9]{1,3},[0-9]{1,3}", port):
            return False
        self.stop_streaming(camera)
        try:
            result = SecureCommandRunner.run_safe(
                ["gphoto2", "--port", port, "--capture-image-and-download", "--filename", output_path,
                 "--force-overwrite", "--keep"], capture_output=True, text=True, timeout=60)
            # The caller reserves a unique path. Preserve native bytes and metadata.
            return result.returncode == 0 and os.path.isfile(output_path) and os.path.getsize(output_path) > 0
        except (OSError, subprocess.SubprocessError):
            # subprocess.run kills/reaps its own child on timeout; never pkill a name.
            log.warning("Native photo capture failed for the selected camera")
            return False
