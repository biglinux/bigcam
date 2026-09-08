"""Virtual camera – v4l2loopback output for OBS / videoconference apps."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from utils.command_runner import SecureCommandRunner
import threading
import uuid


log = logging.getLogger(__name__)

_V4L2LOOPBACK_CTL = shutil.which("v4l2loopback-ctl") or "/usr/sbin/v4l2loopback-ctl"


def _run_privileged(action: str) -> bool:
    if action != "load":
        log.warning("Refusing to unload a potentially shared kernel module")
        return False
    return _helper("load").returncode == 0


def _helper(*args: str) -> subprocess.CompletedProcess:
    """Run only the installed fixed-operation helper, never a user script."""
    try:
        return SecureCommandRunner.run_safe(
            ["pkexec", "/usr/lib/bigcam/virtual-camera-helper", *args],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("Virtual camera authorization failed: %s", exc)
        return subprocess.CompletedProcess(args, 1, "", str(exc))


class VirtualCamera:
    """Manage v4l2loopback virtual camera output.

    Supports multiple simultaneous virtual cameras — one per physical camera.
    Devices are created by the authorized helper and owned by this session.
    Existing devices belonging to other applications are never taken over.
    """

    _loopback_device: str = ""
    _process: subprocess.Popen | None = None
    _load_attempted: bool = False
    _enabled: bool = False
    _dynamic_supported: bool | None = None  # lazy-checked
    _max_devices: int = 5
    _name_template: str = "BigCam Virtual"

    # camera_id → v4l2loopback device path
    _allocations: dict[str, str] = {}
    # Devices created dynamically by v4l2loopback-ctl (need explicit cleanup)
    _dynamic_devices: set[str] = set()
    # Sequential counter for "BigCam Virtual N" naming
    _next_vcam_number: int = 1
    _labels_synced: bool = False
    _alloc_lock = threading.RLock()
    _session_id = uuid.uuid4().hex

    @staticmethod
    def is_available() -> bool:
        return os.path.exists("/usr/lib/modules") and _has_v4l2loopback()

    @staticmethod
    def kernel_status() -> str:
        """Return 'ready', 'kernel_mismatch', or 'not_installed'."""
        if not os.path.exists("/usr/lib/modules"):
            return "not_installed"
        return _v4l2loopback_kernel_status()

    @classmethod
    def _is_dynamic_supported(cls) -> bool:
        return os.path.isfile(_V4L2LOOPBACK_CTL) and os.path.isfile("/usr/lib/bigcam/virtual-camera-helper")

    @staticmethod
    def find_all_loopback_devices() -> list[str]:
        """Return all v4l2loopback devices available."""
        devices: list[str] = []
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--list-devices"],
                capture_output=True,
                text=True,
                timeout=5,
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

    @classmethod
    def _get_device_labels(cls) -> dict[str, str]:
        """Return mapping of device_path → card label for v4l2loopback devices."""
        labels: dict[str, str] = {}
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--list-devices"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return labels
            current_label = ""
            for line in result.stdout.splitlines():
                line_s = line.strip()
                if line_s.startswith("/dev/video"):
                    if current_label:
                        labels[line_s] = current_label
                else:
                    m = re.match(r"^(.+?)\s*\(", line_s)
                    current_label = m.group(1).strip() if m else ""
        except Exception:
            log.debug("Failed to scan device labels", exc_info=True)
        return labels

    @classmethod
    def _get_existing_labels(cls) -> set[str]:
        """Return the set of card labels currently used by v4l2loopback devices."""
        return set(cls._get_device_labels().values())

    @classmethod
    def _sync_vcam_counter(cls) -> None:
        """Advance _next_vcam_number past any existing device labels."""
        if cls._labels_synced:
            return
        cls._labels_synced = True
        labels = cls._get_existing_labels()
        max_n = 0
        pattern = re.compile(re.escape(cls._name_template) + r"\s+(\d+)$")
        for label in labels:
            m = pattern.match(label)
            if m:
                max_n = max(max_n, int(m.group(1)))
        if max_n >= cls._next_vcam_number:
            cls._next_vcam_number = max_n + 1
            log.debug("Synced _next_vcam_number to %d from existing labels", cls._next_vcam_number)

    @staticmethod
    def find_loopback_device() -> str:
        """Return first available v4l2loopback device."""
        devices = VirtualCamera.find_all_loopback_devices()
        return devices[0] if devices else ""

    @classmethod
    def find_free_loopback_device(cls) -> str:
        """Reuse only idle devices created by this process; labels are not ownership."""
        with cls._alloc_lock:
            allocated = set(cls._allocations.values())
            return next((dev for dev in sorted(cls._dynamic_devices)
                         if dev not in allocated and os.path.exists(dev)), "")

    @classmethod
    def _add_dynamic_device(cls, label: str) -> str:
        safe_label = "".join(c if c.isalnum() or c in " ._-" else "_" for c in label)
        safe_label = safe_label.encode("utf-8")[:31].decode("utf-8", "ignore") or "BigCam"
        result = _helper("create", cls._session_id, safe_label)
        device = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r"/dev/video[0-9]{1,3}", device):
            with cls._alloc_lock:
                cls._dynamic_devices.add(device)
            return device
        log.warning("Could not create an owned virtual camera: %s", result.stderr.strip())
        return ""

    @classmethod
    def _delete_dynamic_device(cls, dev: str) -> bool:
        with cls._alloc_lock:
            if dev not in cls._dynamic_devices or dev in cls._allocations.values():
                return False
            result = _helper("delete", cls._session_id, dev)
            if result.returncode == 0:
                cls._dynamic_devices.discard(dev)
                return True
        log.warning("Keeping virtual camera after unsuccessful cleanup: %s", dev)
        return False

    @classmethod
    def allocate_device(cls, camera_id: str) -> str:
        """Allocate a device created by this session, never an unregistered loopback."""
        with cls._alloc_lock:
            if camera_id in cls._allocations:
                return cls._allocations[camera_id]
            if len(cls._allocations) >= cls._max_devices:
                from gi.repository import GLib
                from core.event_bus import event_bus
                GLib.idle_add(event_bus.emit, "vcam-limit-reached", cls._max_devices)
                return ""
            if not cls._is_dynamic_supported():
                log.warning("Install the BigCam Polkit helper and v4l2loopback-ctl to create virtual cameras")
                return ""
            device = cls.find_free_loopback_device()
            if not device:
                device = cls._add_dynamic_device(f"{cls._name_template} {cls._next_vcam_number}")
                if device:
                    cls._next_vcam_number += 1
            if device:
                cls._allocations[camera_id] = device
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
    def cleanup_dynamic_devices(cls) -> None:
        """Delete only released devices created by this session; retain failures."""
        with cls._alloc_lock:
            idle = cls._dynamic_devices - set(cls._allocations.values())
        for device in sorted(idle):
            cls._delete_dynamic_device(device)

    @classmethod
    def reset_all_allocations(cls) -> None:
        """Delete dynamic devices and clear allocations (name template change).

        After calling this, the next allocate_device() calls will create
        new devices with the current name template.
        """
        cls.cleanup_dynamic_devices()

    @classmethod
    def load_module(cls, card_label: str | None = None) -> bool:
        """Load v4l2loopback kernel module.

        The helper uses fixed options and never unloads a shared module.
        """
        return _run_privileged("load")

    @classmethod
    def start(cls, gst_pipeline: str) -> bool:
        """Start writing to the loopback device."""
        device = cls.ensure_ready(camera_id="legacy-pipeline")
        if not device:
            return False
        cls._loopback_device = device

        try:
            cls._process = SecureCommandRunner.popen_safe(
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
    def set_max_devices(cls, n: int) -> None:
        cls._max_devices = min(max(1, n), 8)

    @classmethod
    def get_max_devices(cls) -> int:
        return cls._max_devices

    @classmethod
    def set_name_template(cls, template: str) -> None:
        new_template = template or "BigCam Virtual"
        if new_template == cls._name_template:
            return
        cls._name_template = new_template
        # Reset label sync so the counter re-syncs with existing device names
        cls._labels_synced = False
        cls._next_vcam_number = 1

    @classmethod
    def get_name_template(cls) -> str:
        return cls._name_template

    @classmethod
    def is_running(cls) -> bool:
        return cls._process is not None and cls._process.poll() is None

    @classmethod
    def ensure_ready(cls, card_label: str | None = None, camera_id: str = "") -> str:
        """Ensure v4l2loopback is loaded and return a device for *camera_id*.

        Each camera gets its own dedicated v4l2loopback device so that
        multiple cameras can output to virtual devices simultaneously.
        Devices are created dynamically via v4l2loopback-ctl when possible.

        Only activates when virtual camera is enabled by the user.
        Tries to load the module once per session if not already loaded.
        Returns empty string if unavailable or not enabled.
        """
        if not cls._enabled:
            return ""

        # Check if module is loaded (any loopback devices exist?)
        devices = cls.find_all_loopback_devices()
        module_loaded = len(devices) > 0 or _is_module_loaded()

        if not module_loaded:
            if not cls.is_available():
                return ""
            if not cls._load_attempted:
                cls._load_attempted = True
                cls.load_module(card_label=card_label)
                module_loaded = _is_module_loaded()

        if not module_loaded:
            return ""

        # Module is loaded — allocate a device (creates dynamically if needed)
        if camera_id:
            return cls.allocate_device(camera_id)
        return cls.allocate_device("default")

    @staticmethod
    def _reload_module() -> bool:
        log.warning("Reload requires explicit administrator action; shared devices are not interrupted")
        return False


def _is_module_loaded() -> bool:
    """Check if the v4l2loopback kernel module is currently loaded."""
    return os.path.isdir("/sys/module/v4l2loopback")


def _has_v4l2loopback() -> bool:
    try:
        result = SecureCommandRunner.run_safe(
            ["modinfo", "v4l2loopback"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def _v4l2loopback_pkg_installed() -> bool:
    """Check if v4l2loopback DKMS package is installed (even if not built for current kernel)."""
    for pkg in ("v4l2loopback-dkms", "v4l2loopback"):
        try:
            result = subprocess.run(
                ["pacman", "-Q", pkg],
                capture_output=True,
                timeout=5,
            )
            if result.returncode == 0:
                return True
        except FileNotFoundError:
            pass
    return False


def _v4l2loopback_kernel_status() -> str:
    """Return the status of v4l2loopback for the current kernel.

    Returns one of:
        "ready"            – modinfo finds the module (can be loaded)
        "kernel_mismatch"  – package installed but module not built for running kernel
        "not_installed"    – package not found at all
    """
    if _has_v4l2loopback():
        return "ready"
    if _v4l2loopback_pkg_installed():
        return "kernel_mismatch"
    return "not_installed"


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
