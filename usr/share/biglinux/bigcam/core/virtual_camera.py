"""Virtual camera – v4l2loopback output for OBS / videoconference apps."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading


log = logging.getLogger(__name__)

_HELPER_SCRIPT = "/usr/share/biglinux/bigcam/script/load-v4l2loopback.sh"


def _run_privileged(action: str) -> bool:
    """Run modprobe via passwordless sudo (sudoers.d/bigcam)."""
    cmd = _modprobe_args(action)
    result = subprocess.run(
        ["sudo", "-n", *cmd],
        capture_output=True,
        timeout=15,
    )
    if result.returncode != 0:
        log.error(
            "sudo -n modprobe failed (rc=%d): %s",
            result.returncode,
            result.stderr.decode(errors="replace").strip(),
        )
    return result.returncode == 0


def _modprobe_args(action: str) -> list[str]:
    """Return the modprobe argument list for the given action."""
    _modprobe = shutil.which("modprobe") or "/usr/bin/modprobe"
    if action == "unload":
        return [_modprobe, "-r", "v4l2loopback"]
    # load / reload — create 4 devices for multi-camera support
    # exclusive_caps must be set per-device (1,1,1,1) for all to work in WebRTC
    # NOTE: Do NOT add quotes around card_label values — subprocess passes
    # arguments directly (no shell), so quotes would become literal characters.
    return [
        _modprobe,
        "v4l2loopback",
        "devices=4",
        "exclusive_caps=1,1,1,1",
        "max_buffers=4",
        "video_nr=10,11,12,13",
        "card_label=BigCam Virtual 1,BigCam Virtual 2,"
        "BigCam Virtual 3,BigCam Virtual 4",
    ]


_LOOPBACK_DEVICES = ["/dev/video10", "/dev/video11", "/dev/video12", "/dev/video13"]


class VirtualCamera:
    """Manage v4l2loopback virtual camera output.

    Supports multiple simultaneous virtual cameras — one per physical camera.
    Each camera is allocated a separate v4l2loopback device.
    """

    _loopback_device: str = ""
    _process: subprocess.Popen | None = None
    _load_attempted: bool = False
    _enabled: bool = False

    # camera_id → v4l2loopback device path
    _allocations: dict[str, str] = {}
    _alloc_lock = threading.RLock()

    @staticmethod
    def is_available() -> bool:
        return os.path.exists("/usr/lib/modules") and _has_v4l2loopback()

    @staticmethod
    def find_all_loopback_devices() -> list[str]:
        """Return all v4l2loopback devices available."""
        devices: list[str] = []
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--list-devices"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                return devices
            lines = result.stdout.splitlines()
            for i, line in enumerate(lines):
                if "v4l2loopback" in line.lower() or "bigcam" in line.lower():
                    for j in range(i + 1, len(lines)):
                        dev = lines[j].strip()
                        if not dev.startswith("/dev/video"):
                            break
                        devices.append(dev)
        except Exception:
            log.debug("v4l2loopback device scan failed", exc_info=True)
        return devices

    @staticmethod
    def find_loopback_device() -> str:
        """Return first available v4l2loopback device."""
        for dev in _LOOPBACK_DEVICES:
            try:
                result = subprocess.run(
                    ["v4l2-ctl", "-d", dev, "--info"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0 and "v4l2 loopback" in result.stdout.lower():
                    return dev
            except Exception:
                continue
        # Fallback: scan
        devices = VirtualCamera.find_all_loopback_devices()
        return devices[0] if devices else ""

    @classmethod
    def find_free_loopback_device(cls) -> str:
        """Return a v4l2loopback device not currently allocated to any camera."""
        with cls._alloc_lock:
            allocated = set(cls._allocations.values())
        devices = cls.find_all_loopback_devices()
        if not devices:
            # Try known paths directly
            for dev in _LOOPBACK_DEVICES:
                if dev not in allocated and os.path.exists(dev):
                    devices.append(dev)
        for dev in devices:
            if dev not in allocated:
                return dev
        return ""

    @classmethod
    def allocate_device(cls, camera_id: str) -> str:
        """Allocate a v4l2loopback device for a camera. Returns device path."""
        with cls._alloc_lock:
            # Already allocated?
            if camera_id in cls._allocations:
                return cls._allocations[camera_id]
            device = cls.find_free_loopback_device()
            if device:
                cls._allocations[camera_id] = device
                log.debug("Allocated %s for camera %s", device, camera_id)
            return device

    @classmethod
    def release_device(cls, camera_id: str) -> None:
        """Release the v4l2loopback device allocated to a camera."""
        with cls._alloc_lock:
            dev = cls._allocations.pop(camera_id, None)
        if dev:
            log.debug("Released %s from camera %s", dev, camera_id)

    @classmethod
    def get_device_for_camera(cls, camera_id: str) -> str:
        """Return the allocated device for a camera, or empty string."""
        with cls._alloc_lock:
            return cls._allocations.get(camera_id, "")

    @classmethod
    def load_module(cls, card_label: str | None = None) -> bool:
        """Load v4l2loopback kernel module with 2 devices.

        Device 10: BigCam virtual camera output (for sharing)
        Device 11: Reserved for gPhoto2 streaming

        Uses pkexec (Polkit) with ``auth_admin_keep`` so the user only
        authenticates once per desktop session.  Falls back to sudo.
        """
        return _run_privileged("load")

    @classmethod
    def start(cls, gst_pipeline: str) -> bool:
        """Start writing to the loopback device."""
        device = cls.find_loopback_device()
        if not device:
            if not cls.load_module():
                return False
            device = cls.find_loopback_device()
            if not device:
                return False
        cls._loopback_device = device

        try:
            cls._process = subprocess.Popen(
                [
                    "gst-launch-1.0",
                    *gst_pipeline.split(),
                    "!",
                    "v4l2sink",
                    f"device={device}",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:
            return False

    @classmethod
    def stop(cls) -> None:
        if cls._process is not None:
            cls._process.terminate()
            try:
                cls._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls._process.kill()
            cls._process = None

    @classmethod
    def set_enabled(cls, enabled: bool) -> None:
        cls._enabled = enabled

    @classmethod
    def is_enabled(cls) -> bool:
        return cls._enabled

    @classmethod
    def is_running(cls) -> bool:
        return cls._process is not None and cls._process.poll() is None

    @classmethod
    def ensure_ready(cls, card_label: str | None = None, camera_id: str = "") -> str:
        """Ensure v4l2loopback is loaded and return a device for *camera_id*.

        Each camera gets its own dedicated v4l2loopback device so that
        multiple cameras can output to virtual devices simultaneously.

        Only activates when virtual camera is enabled by the user.
        Tries to load the module once per session if not already loaded.
        Returns empty string if unavailable or not enabled.
        """
        if not cls._enabled:
            return ""
        devices = cls.find_all_loopback_devices()
        has_caps = _has_exclusive_caps()
        has_enough = len(devices) >= 4

        if devices and has_caps and has_enough:
            if camera_id:
                return cls.allocate_device(camera_id)
            return devices[0]

        # Module not loaded, or loaded with wrong params — try to (re)load
        if not cls.is_available():
            if camera_id and devices:
                return cls.allocate_device(camera_id)
            return devices[0] if devices else ""

        if not cls._load_attempted:
            cls._load_attempted = True
            if not has_caps or not has_enough:
                log.info(
                    "v4l2loopback reload needed: devices=%d, exclusive_caps=%s",
                    len(devices), has_caps,
                )
                if devices:
                    cls._reload_module()
                else:
                    cls.load_module(card_label=card_label)

        if camera_id:
            return cls.allocate_device(camera_id)
        return cls.find_loopback_device()

    @staticmethod
    def _reload_module() -> bool:
        """Unload and reload v4l2loopback with correct parameters."""
        _run_privileged("unload")
        return _run_privileged("load")


def _has_v4l2loopback() -> bool:
    try:
        result = subprocess.run(
            ["modinfo", "v4l2loopback"],
            capture_output=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def _has_exclusive_caps() -> bool:
    """Check if ALL loaded v4l2loopback devices have exclusive_caps enabled."""
    try:
        with open("/sys/module/v4l2loopback/parameters/exclusive_caps") as f:
            raw = f.read().strip()
    except (FileNotFoundError, OSError):
        return False
    entries = [v.strip() for v in raw.split(",") if v.strip()]
    # Count actual devices from video_nr (the 'devices' param is not
    # always exposed in sysfs depending on kernel/module version).
    try:
        with open("/sys/module/v4l2loopback/parameters/video_nr") as f:
            vn = f.read().strip()
        # video_nr contains entries like "10,11,12,13,-1,-1,-1,-1"
        # -1 means unused slot, so filter them out.
        n_devices = len([v for v in vn.split(",") if v.strip() and v.strip() != "-1"])
    except (FileNotFoundError, OSError, ValueError):
        # Fallback: assume we need 4 devices with exclusive_caps
        n_devices = 4
    active = entries[:n_devices]
    return len(active) >= n_devices and all(v == "Y" for v in active)
