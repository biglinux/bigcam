"""GPhoto2 backend – covers 2 500+ DSLR and mirrorless cameras."""

from __future__ import annotations

import logging
import os
import re
import signal
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
    # Track active streaming sessions per camera port
    _active_streams: dict[str, str] = {}  # port -> udp_port

    def get_backend_type(self) -> BackendType:
        return BackendType.GPHOTO2

    @staticmethod
    def _kill_gvfs() -> None:
        """Kill GVFS processes that interfere with gphoto2 USB access."""
        subprocess.run(
            ["systemctl", "--user", "stop", "gvfs-gphoto2-volume-monitor.service"],
            capture_output=True,
        )
        subprocess.run(
            ["systemctl", "--user", "mask", "gvfs-gphoto2-volume-monitor.service"],
            capture_output=True,
        )
        subprocess.run(
            ["pkill", "-9", "-f", "gvfs-gphoto2-volume-monitor"],
            capture_output=True,
        )
        subprocess.run(
            ["pkill", "-9", "-f", "gvfsd-gphoto2"],
            capture_output=True,
        )
        subprocess.run(
            ["gio", "mount", "-u", "gphoto2://"],
            capture_output=True,
        )

    @staticmethod
    def _release_usb_device(port: str) -> None:
        """Kill any process (GVFS etc.) holding the USB device so gphoto2 can open it."""
        try:
            bus, dev = port.replace("usb:", "").split(",")
            usb_path = f"/dev/bus/usb/{bus}/{dev}"
            if not os.path.exists(usb_path):
                return
            result = subprocess.run(
                ["fuser", usb_path],
                capture_output=True, text=True, timeout=5,
            )
            pids = result.stdout.strip().split()
            for pid_str in pids:
                pid_str = pid_str.strip().rstrip(":")
                if not pid_str.isdigit():
                    continue
                pid = int(pid_str)
                # Skip our own process
                if pid == os.getpid():
                    continue
                try:
                    cmdline_path = f"/proc/{pid}/cmdline"
                    with open(cmdline_path) as f:
                        cmdline = f.read()
                    print(f"[DEBUG] PID {pid} holding {usb_path}: {cmdline[:120]}")
                    os.kill(pid, signal.SIGKILL)
                    print(f"[DEBUG] Killed PID {pid}")
                except (ProcessLookupError, FileNotFoundError, PermissionError):
                    pass
            if pids:
                time.sleep(1)
        except Exception:
            pass

    @staticmethod
    def _diagnose_usb(port: str) -> None:
        """Print diagnostic info about a USB device for debugging."""
        try:
            bus, dev = port.replace("usb:", "").split(",")
            usb_path = f"/dev/bus/usb/{bus}/{dev}"

            # Check device existence and permissions
            exists = os.path.exists(usb_path)
            print(f"[DEBUG] USB diag: {usb_path} exists={exists}")
            if not exists:
                # Show what devices ARE on this bus
                bus_dir = f"/dev/bus/usb/{bus}"
                if os.path.isdir(bus_dir):
                    devs = sorted(os.listdir(bus_dir))
                    print(f"[DEBUG] USB diag: devices on bus {bus}: {devs}")
                return

            # Check file permissions
            import stat
            st = os.stat(usb_path)
            mode = stat.filemode(st.st_mode)
            print(f"[DEBUG] USB diag: {usb_path} mode={mode} uid={st.st_uid} gid={st.st_gid}")

            # Check lsusb for this specific device
            result = subprocess.run(
                ["lsusb", "-s", f"{bus}:{dev}"],
                capture_output=True, text=True, timeout=5,
            )
            print(f"[DEBUG] USB diag lsusb: {result.stdout.strip()}")

            # Check fuser
            result = subprocess.run(
                ["fuser", usb_path],
                capture_output=True, text=True, timeout=5,
            )
            holders = result.stdout.strip()
            print(f"[DEBUG] USB diag fuser: '{holders}'")

            # Check gphoto2 --auto-detect
            result = subprocess.run(
                ["gphoto2", "--auto-detect"],
                capture_output=True, text=True, timeout=10,
            )
            lines = [l.strip() for l in result.stdout.strip().splitlines()[2:] if l.strip()]
            print(f"[DEBUG] USB diag auto-detect: {lines}")

            # Check dmesg for recent USB errors on this bus
            result = subprocess.run(
                ["dmesg", "--time-format=reltime"],
                capture_output=True, text=True, timeout=5,
            )
            usb_errors = [l for l in result.stdout.splitlines()[-50:]
                         if f"usb {bus.lstrip('0') or '0'}-" in l.lower()
                         or "error" in l.lower() and "usb" in l.lower()]
            if usb_errors:
                print(f"[DEBUG] USB diag dmesg errors: {usb_errors[-5:]}")
        except Exception as exc:
            print(f"[DEBUG] USB diag error: {exc}")

    def is_available(self) -> bool:
        try:
            subprocess.run(["gphoto2", "--version"], capture_output=True, check=True)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError):
            return False

    # -- detection -----------------------------------------------------------

    _streaming_active = False
    _last_detected: list[CameraInfo] = []

    def detect_cameras(self) -> list[CameraInfo]:
        # While streaming, return previously detected cameras
        if self._streaming_active:
            return self._last_detected

        cameras: list[CameraInfo] = []
        try:
            # Kill GVFS to release the camera
            subprocess.run(
                ["pkill", "-f", "gvfs-gphoto2-volume-monitor"],
                capture_output=True,
            )
            time.sleep(1)

            # Retry up to 2 times in case GVFS hasn't released the device yet
            for attempt in range(2):
                result = subprocess.run(
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
                time.sleep(1)
        except Exception:
            pass
        if cameras:
            self._last_detected = cameras
        return cameras

    # -- controls ------------------------------------------------------------

    @classmethod
    def _refresh_port(cls, camera: CameraInfo) -> str:
        """Re-detect the current USB port for a camera (device number may change)."""
        old_port = camera.extra.get("port", camera.device_path)
        try:
            result = subprocess.run(
                ["gphoto2", "--auto-detect"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return old_port

            for line in result.stdout.strip().splitlines()[2:]:
                line = line.strip()
                if not line or "usb:" not in line:
                    continue
                parts = line.split("usb:")
                if len(parts) < 2:
                    continue
                name = parts[0].strip()
                port = "usb:" + parts[1].strip()
                # Match by camera model name
                if name and name in camera.name:
                    if port != old_port:
                        print(f"[DEBUG] Port changed: {old_port} -> {port}")
                        camera.extra["port"] = port
                        camera.device_path = port
                        camera.id = f"gphoto2:{port}"
                    return port
        except Exception:
            pass
        return old_port

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
        print(f"[DEBUG] get_controls: port={port}")

        # Ensure GVFS is dead and USB device is free
        self._kill_gvfs()
        self._release_usb_device(port)

        # Diagnostic: check USB device accessibility
        self._diagnose_usb(port)

        delays = [0, 3, 5]
        try:
            for attempt, delay in enumerate(delays, 1):
                if delay:
                    print(f"[DEBUG] get_controls: waiting {delay}s before retry...")
                    time.sleep(delay)
                    self._kill_gvfs()
                    self._release_usb_device(port)
                    # Re-diagnose after wait
                    self._diagnose_usb(port)

                print(f"[DEBUG] get_controls attempt {attempt}/{len(delays)}")
                result = subprocess.run(
                    ["gphoto2", "--port", port, "--list-all-config"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                stdout_preview = result.stdout[:300] if result.returncode != 0 else ""
                print(f"[DEBUG] --list-all-config rc={result.returncode}, "
                      f"stdout_lines={len(result.stdout.splitlines())}, "
                      f"stderr={result.stderr.strip()[:200]}")
                if result.returncode != 0 and stdout_preview:
                    print(f"[DEBUG] stdout preview: {stdout_preview}")
                if result.returncode == 0 and result.stdout.strip():
                    break
            else:
                # Last resort: re-detect port and try once more
                port = self._refresh_port(camera)
                print(f"[DEBUG] get_controls fallback port={port}")
                self._release_usb_device(port)
                self._diagnose_usb(port)
                result = subprocess.run(
                    ["gphoto2", "--port", port, "--list-all-config"],
                    capture_output=True, text=True, timeout=15,
                )
                if result.returncode != 0 or not result.stdout.strip():
                    print("[DEBUG] get_controls: all attempts failed")
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
        """Launch the gphoto2 streaming script (persistent session per camera)."""
        self._streaming_active = True
        # Refresh USB port (device number may change after GVFS kill)
        port = self._refresh_port(camera)
        udp_port = str(camera.extra.get("udp_port", 5000))

        # If this camera is already streaming, just return success
        if port in self._active_streams:
            print(f"[DEBUG] Camera {camera.name} already streaming on port {port}")
            return True

        # Kill GVFS and release USB device before streaming
        self._kill_gvfs()
        self._release_usb_device(port)

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
            with tempfile.TemporaryFile() as f:
                res = subprocess.run(
                    [script, port_arg, udp_port, camera.name],
                    stdout=f,
                    stderr=subprocess.STDOUT,
                )
                f.seek(0)
                raw = f.read()
                output = raw.decode("utf-8", errors="replace").strip()
            log.info("gphoto2 script output:\n%s", output)

            if res.returncode == 0:
                for line in output.split("\n"):
                    if line.startswith("SUCCESS:"):
                        dev = line.split("SUCCESS:")[1].strip()
                        log.info("GPhoto2 streaming started on %s", dev)
                        self._active_streams[port] = udp_port
                        return True
                log.info("GPhoto2 script exited 0 (no explicit SUCCESS)")
                self._active_streams[port] = udp_port
                return True

            log.error("GPhoto2 script failed (code %d): %s",
                      res.returncode, output)
            self._streaming_active = False
            return False
        except Exception as exc:
            log.error("Failed to start gphoto2 streaming: %s", exc)
            self._streaming_active = False
            return False

    def stop_streaming(self, camera: CameraInfo | None = None) -> None:
        """Stop gphoto2/ffmpeg processes for a specific camera, or all if None."""
        self._streaming_active = False
        try:
            if camera:
                port = camera.extra.get("port", camera.device_path)
                udp_port = str(camera.extra.get("udp_port", 5000))
                self._active_streams.pop(port, None)

                # Graceful SIGTERM first — gives gphoto2 time to close PTP session
                subprocess.run(
                    ["pkill", "-f", f"gphoto2.*--port {port}"],
                    capture_output=True,
                )
                subprocess.run(
                    ["pkill", "-f", f"ffmpeg.*udp://127.0.0.1:{udp_port}"],
                    capture_output=True,
                )
                time.sleep(2)
                # Force-kill any survivors
                subprocess.run(
                    ["pkill", "-9", "-f", f"gphoto2.*--port {port}"],
                    capture_output=True,
                )
                subprocess.run(
                    ["pkill", "-9", "-f", f"ffmpeg.*udp://127.0.0.1:{udp_port}"],
                    capture_output=True,
                )
            else:
                self._active_streams.clear()
                subprocess.run(["pkill", "-f", "gphoto2 --"],
                               capture_output=True)
                time.sleep(1)
                subprocess.run(["pkill", "-9", "-f", "gphoto2 --"],
                               capture_output=True)
                subprocess.run(["pkill", "-9", "-f", "ffmpeg.*mpegts"],
                               capture_output=True)
                subprocess.run(["pkill", "-9", "-f", "ffmpeg.*v4l2"],
                               capture_output=True)
        except Exception:
            pass
        self._streaming_process = None

        # Kill GVFS immediately after stopping — prevents it from re-grabbing cameras
        self._kill_gvfs()
        time.sleep(1)

    def needs_streaming_setup(self) -> bool:
        """GPhoto2 requires an external streaming process."""
        return True

    def is_camera_streaming(self, camera: CameraInfo) -> bool:
        """Check if a specific camera already has an active streaming session."""
        port = camera.extra.get("port", camera.device_path)
        if port not in self._active_streams:
            return False
        # Verify the process is actually alive
        udp_port = self._active_streams[port]
        result = subprocess.run(
            ["pgrep", "-f", f"gphoto2.*--port {port}"],
            capture_output=True,
        )
        if result.returncode != 0:
            # Process died — clean up
            self._active_streams.pop(port, None)
            return False
        return True

    # -- photo ---------------------------------------------------------------

    def can_capture_photo(self) -> bool:
        return True

    def capture_photo(self, camera: CameraInfo, output_path: str) -> bool:
        port = camera.extra.get("port", camera.device_path)
        for attempt in range(2):
            try:
                self._kill_gvfs()
                if attempt > 0:
                    self._release_usb_device(port)
                    time.sleep(2)

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
                if os.path.isfile(output_path):
                    return True
            except Exception as exc:
                log.warning("capture_photo attempt %d failed: %s", attempt + 1, exc)
                if attempt == 0:
                    time.sleep(1)
        return False


