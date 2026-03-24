"""scrcpy camera – Android camera streaming via scrcpy + ADB."""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import threading
from typing import Optional

import gi

gi.require_version("Gst", "1.0")

from gi.repository import GLib, GObject, Gst

log = logging.getLogger(__name__)

_SCRCPY_BIN = "scrcpy"
_ADB_BIN = "adb"


class DeviceInfo:
    """Descriptor for a connected Android device."""

    __slots__ = ("serial", "model", "state", "transport")

    def __init__(
        self,
        serial: str,
        model: str = "",
        state: str = "device",
        transport: str = "usb",
    ) -> None:
        self.serial = serial
        self.model = model or serial
        self.state = state
        self.transport = transport  # "usb" or "tcpip"

    def __repr__(self) -> str:
        return f"DeviceInfo({self.serial!r}, model={self.model!r}, {self.transport})"


class ScrcpyCamera(GObject.Object):
    """Manage scrcpy subprocess for camera streaming from Android devices.

    Launches ``scrcpy --video-source=camera`` and provides the stdout pipe
    file descriptor for GStreamer ``fdsrc`` to read raw H.264 directly.
    """

    __gsignals__ = {
        "connected": (GObject.SignalFlags.RUN_LAST, None, (int, int)),
        "disconnected": (GObject.SignalFlags.RUN_LAST, None, ()),
        "status-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }

    def __init__(self) -> None:
        super().__init__()
        self._process: Optional[subprocess.Popen] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._v4l2_device: str = ""
        self._device_serial: str = ""
        self._running = False

    @property
    def pid(self) -> int | None:
        """Return the PID of the scrcpy process, or None."""
        proc = self._process
        return proc.pid if proc else None

    # -- availability --------------------------------------------------------

    @staticmethod
    def is_available() -> bool:
        """Return True if scrcpy and adb binaries are on PATH."""
        return (
            shutil.which(_SCRCPY_BIN) is not None
            and shutil.which(_ADB_BIN) is not None
        )

    @staticmethod
    def scrcpy_version() -> str:
        """Return the scrcpy version string or empty on failure."""
        try:
            out = subprocess.run(
                [_SCRCPY_BIN, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            m = re.search(r"(\d+\.\d+(\.\d+)?)", out.stdout)
            return m.group(1) if m else ""
        except Exception:
            return ""

    # -- device discovery ----------------------------------------------------

    @staticmethod
    def ensure_adb_server() -> bool:
        """Start the ADB server if not already running. Returns True on success."""
        try:
            subprocess.run(
                [_ADB_BIN, "start-server"],
                capture_output=True,
                timeout=10,
            )
            return True
        except Exception as exc:
            log.warning("adb start-server failed: %s", exc)
            return False

    @staticmethod
    def list_devices(include_unauthorized: bool = False) -> list[DeviceInfo]:
        """List connected Android devices via ``adb devices -l``.

        When *include_unauthorized* is True, devices with state
        ``unauthorized`` or ``no permissions`` are also returned so
        callers can show better diagnostics.
        """
        try:
            result = subprocess.run(
                [_ADB_BIN, "devices", "-l"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception as exc:
            log.warning("adb devices failed: %s", exc)
            return []

        accepted_states = {"device"}
        if include_unauthorized:
            accepted_states |= {"unauthorized", "no"}  # "no permissions"

        devices: list[DeviceInfo] = []
        for line in result.stdout.splitlines()[1:]:
            line = line.strip()
            if not line or "List of" in line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial = parts[0]
            state = parts[1]
            # Handle "no permissions" which is two words
            if state == "no" and len(parts) > 2 and parts[2].startswith("permissions"):
                state = "unauthorized"
            if state not in accepted_states:
                continue
            model = serial
            transport = "tcpip" if ":" in serial else "usb"
            for part in parts[2:]:
                if part.startswith("model:"):
                    model = part.split(":", 1)[1].replace("_", " ")
                    break
            # For authorized devices, try to get the marketing name
            if state == "device" and (model == serial or model.startswith("2")):
                try:
                    name_result = subprocess.run(
                        [_ADB_BIN, "-s", serial, "shell",
                         "getprop", "ro.product.marketname"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    mname = name_result.stdout.strip()
                    if mname:
                        model = mname
                except Exception:
                    pass
            devices.append(DeviceInfo(serial, model, state, transport))
        return devices

    @staticmethod
    def detect_android_usb() -> list[dict[str, str]]:
        """Detect Android devices physically connected via USB using lsusb.

        Returns a list of dicts with 'vendor_id', 'product_id', and 'name'.
        This works even when ADB does not see the device (e.g. USB Debugging
        is disabled or the device hasn't authorized the host).
        """
        # Common Android vendor IDs
        android_vendors = {
            "04e8",  # Samsung
            "18d1",  # Google / Nexus / Pixel
            "2717",  # Xiaomi / POCO / Redmi
            "0bb4",  # HTC
            "12d1",  # Huawei
            "1004",  # LG
            "22b8",  # Motorola
            "054c",  # Sony
            "2a70",  # OnePlus
            "19d2",  # ZTE
            "22d9",  # OPPO
            "2d95",  # Vivo / BBK
            "2ae5",  # Realme
            "1532",  # Razer
            "0502",  # Acer
            "0b05",  # ASUS
            "17ef",  # Lenovo
            "2a45",  # Meizu
            "1949",  # Amazon / Fire
            "2916",  # Yota
            "2341",  # Arduino (exclude)
            "29a9",  # Fairphone
            "1bbb",  # T-Mobile
            "0fce",  # Sony Ericsson
            "2b4c",  # Nokia/HMD
        }
        found: list[dict[str, str]] = []
        try:
            result = subprocess.run(
                ["lsusb"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in result.stdout.splitlines():
                # Format: "Bus 001 Device 003: ID 2717:ff48 Xiaomi Inc. ..."
                m = re.search(r"ID\s+([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\s*(.*)", line)
                if m:
                    vid = m.group(1).lower()
                    if vid in android_vendors:
                        found.append({
                            "vendor_id": vid,
                            "product_id": m.group(2).lower(),
                            "name": m.group(3).strip(),
                        })
        except Exception as exc:
            log.warning("lsusb failed: %s", exc)
        return found

    @staticmethod
    def get_device_ip(serial: str) -> str:
        """Get the WiFi IP address of a device connected via USB."""
        try:
            result = subprocess.run(
                [_ADB_BIN, "-s", serial, "shell", "ip", "route"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            # Look for "wlan0" route: e.g. "192.168.1.0/24 dev wlan0 ... src 192.168.1.100"
            for line in result.stdout.splitlines():
                if "wlan0" in line:
                    m = re.search(r"src\s+([\d.]+)", line)
                    if m:
                        return m.group(1)
        except Exception as exc:
            log.warning("Failed to get device IP: %s", exc)
        return ""

    @staticmethod
    def pair_wifi(host_port: str, pairing_code: str) -> tuple[bool, str]:
        """Pair with a device via WiFi (Wireless Debugging).

        Android 11+ supports ``adb pair HOST:PORT CODE``.
        After pairing, connects with ``adb connect HOST:PORT``.

        Returns (success, message).
        """
        host_port = host_port.strip()
        pairing_code = pairing_code.strip()
        if not host_port or not pairing_code:
            return False, "IP:PORT and pairing code are required"

        try:
            result = subprocess.run(
                [_ADB_BIN, "pair", host_port, pairing_code],
                capture_output=True,
                text=True,
                timeout=15,
            )
            output = (result.stdout + result.stderr).strip()
            if "successfully" not in output.lower():
                return False, f"Pairing failed: {output}"
            log.info("WiFi pairing successful: %s", host_port)
        except Exception as exc:
            return False, f"Pairing error: {exc}"

        # After pairing, connect to the device's debug port (usually 5555
        # or the port shown in Wireless Debugging settings).
        # Extract the IP from host_port to try connecting.
        ip = host_port.split(":")[0]
        # Try connecting on the standard debug port first
        for port in ("5555",):
            target = f"{ip}:{port}"
            try:
                result = subprocess.run(
                    [_ADB_BIN, "connect", target],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                output = (result.stdout + result.stderr).strip()
                if "connected" in output.lower():
                    log.info("Connected via WiFi after pairing: %s", target)
                    return True, f"Paired and connected to {target}"
            except Exception:
                pass

        return True, f"Paired successfully. Connect manually if needed."

    @staticmethod
    def switch_to_wifi(serial: str) -> tuple[bool, str]:
        """Switch a USB-connected device to WiFi ADB mode.

        Returns (success, ip_or_error_message).
        """
        ip = ScrcpyCamera.get_device_ip(serial)
        if not ip:
            return False, "Could not detect phone WiFi IP"

        # Enable ADB over TCP
        try:
            result = subprocess.run(
                [_ADB_BIN, "-s", serial, "tcpip", "5555"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return False, f"adb tcpip failed: {result.stderr.strip()}"
        except Exception as exc:
            return False, str(exc)

        # Connect via WiFi
        import time
        time.sleep(1)  # Give the device time to switch
        target = f"{ip}:5555"
        try:
            result = subprocess.run(
                [_ADB_BIN, "connect", target],
                capture_output=True,
                text=True,
                timeout=10,
            )
            output = (result.stdout + result.stderr).strip()
            if "connected" in output.lower():
                log.info("Switched to WiFi ADB: %s", target)
                return True, ip
            return False, f"adb connect failed: {output}"
        except Exception as exc:
            return False, str(exc)

    @staticmethod
    def list_cameras(device_serial: str) -> list[dict]:
        """List cameras on a device via ``scrcpy --list-cameras``.

        Returns list of dicts with keys: id, facing, size.
        """
        try:
            result = subprocess.run(
                [_SCRCPY_BIN, "--list-cameras", "-s", device_serial],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception as exc:
            log.warning("scrcpy --list-cameras failed: %s", exc)
            return []

        # Parse scrcpy output:  --camera-id=0    (facing=back, size=4000x3000)
        cameras: list[dict] = []
        output = result.stdout + result.stderr
        for m in re.finditer(
            r"--camera-id=(\d+)\s+\(facing=(\w+),\s*size=(\d+x\d+)\)", output
        ):
            cameras.append(
                {"id": m.group(1), "facing": m.group(2), "size": m.group(3)}
            )
        return cameras

    # -- streaming -----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def v4l2_device(self) -> str:
        """v4l2loopback device path scrcpy is writing to."""
        return self._v4l2_device

    @property
    def device_serial(self) -> str:
        return self._device_serial

    def start(
        self,
        device_serial: str,
        v4l2_device: str,
        camera_id: str | None = None,
        camera_facing: str = "back",
        resolution: str | None = None,
        fps: int = 30,
        bitrate: str = "16M",
        max_size: int = 1920,
    ) -> bool:
        """Start scrcpy camera streaming.

        Launches scrcpy with ``--v4l2-sink`` to write decoded frames
        directly into a v4l2loopback device.  BigCam then reads from
        that device with a standard ``v4l2src`` pipeline.

        Args:
            v4l2_device: Path to the v4l2loopback device (e.g. /dev/video10).
        """
        if self._running:
            log.warning("ScrcpyCamera already running")
            return True

        cmd: list[str] = [
            "stdbuf",
            "-oL",
            _SCRCPY_BIN,
            "--video-source=camera",
            "--no-window",
            "--audio-source=mic",
            "--audio-buffer=80",
            "--audio-output-buffer=50",
            f"--v4l2-sink={v4l2_device}",
            f"-b{bitrate}",
            f"--max-fps={fps}",
            "-s",
            device_serial,
        ]

        if max_size > 0:
            cmd.insert(-2, f"-m{max_size}")

        if camera_id is not None:
            cmd.append(f"--camera-id={camera_id}")
        else:
            cmd.append(f"--camera-facing={camera_facing}")

        if resolution:
            cmd.append(f"--camera-size={resolution}")

        log.info("Starting scrcpy: %s", " ".join(cmd))

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError:
            log.error("scrcpy binary not found")
            GLib.idle_add(self.emit, "status-changed", "error")
            return False
        except Exception as exc:
            log.error("Failed to start scrcpy: %s", exc)
            GLib.idle_add(self.emit, "status-changed", "error")
            return False

        self._v4l2_device = v4l2_device
        self._device_serial = device_serial
        self._running = True
        GLib.idle_add(self.emit, "status-changed", "starting")

        # Monitor stderr for status and detect exit
        self._monitor_thread = threading.Thread(
            target=self._monitor_output, daemon=True
        )
        self._monitor_thread.start()

        return True

    def stop(self) -> None:
        """Stop the scrcpy subprocess."""
        proc = self._process
        self._process = None
        self._running = False
        self._v4l2_device = ""
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
            except Exception:
                pass
        GLib.idle_add(self.emit, "status-changed", "stopped")
        GLib.idle_add(self.emit, "disconnected")
        log.info("scrcpy stopped")

    def _read_v4l2_resolution(self) -> tuple[int, int]:
        """Read the current resolution from the v4l2loopback device."""
        if not self._v4l2_device:
            return 0, 0
        try:
            result = subprocess.run(
                ["v4l2-ctl", "-d", self._v4l2_device, "--get-fmt-video"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            for line in result.stdout.splitlines():
                m = re.search(r"Width/Height\s*:\s*(\d+)/(\d+)", line)
                if m:
                    return int(m.group(1)), int(m.group(2))
        except Exception:
            pass
        return 0, 0

    def _monitor_output(self) -> None:
        """Read scrcpy combined stdout+stderr to detect status changes."""
        proc = self._process
        if not proc or not proc.stdout:
            return

        connected = False
        try:
            while True:
                raw_line = proc.stdout.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                log.debug("scrcpy: %s", line)

                # scrcpy logs "v4l2 sink started" when writing to the device
                if "v4l2 sink started" in line or "Texture:" in line or "New stream" in line:
                    if not connected:
                        connected = True
                        w, h = self._read_v4l2_resolution()
                        GLib.idle_add(self.emit, "connected", w, h)
                        GLib.idle_add(self.emit, "status-changed", "connected")

                elif "ERROR" in line or "error" in line.lower():
                    log.warning("scrcpy error: %s", line)

        except Exception as exc:
            log.debug("scrcpy output monitor: %s", exc)
        finally:
            if self._running:
                self._running = False
                GLib.idle_add(self.emit, "disconnected")
                GLib.idle_add(self.emit, "status-changed", "disconnected")
                log.info("scrcpy process exited")
