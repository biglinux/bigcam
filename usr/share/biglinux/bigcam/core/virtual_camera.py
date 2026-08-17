"""Virtual camera – v4l2loopback output for OBS / videoconference apps."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from utils.command_runner import SecureCommandRunner
import threading


log = logging.getLogger(__name__)

_V4L2LOOPBACK_CTL = shutil.which("v4l2loopback-ctl") or "/usr/sbin/v4l2loopback-ctl"


def _run_privileged(action: str) -> bool:
    """Run modprobe via passwordless sudo (sudoers.d/bigcam)."""
    cmd = _modprobe_args(action)
    result = SecureCommandRunner.run_safe(
        ["sudo", "-n", *cmd],
        capture_output=True,
        timeout=15,
    )
    if result.returncode != 0:
        log.error(
            "sudo -n modprobe failed (rc=%d): %s",
            result.returncode,
            result.stderr.strip(),
        )
    return result.returncode == 0


def _modprobe_args(action: str) -> list[str]:
    """Return the modprobe argument list for the given action."""
    _modprobe = shutil.which("modprobe") or "/usr/bin/modprobe"
    if action == "unload":
        return [_modprobe, "-r", "v4l2loopback"]
    # Load module with no initial devices — devices are created dynamically
    # via v4l2loopback-ctl add. Fall back to fixed devices if ctl unavailable.
    if os.path.isfile(_V4L2LOOPBACK_CTL):
        return [_modprobe, "v4l2loopback", "devices=0"]
    max_devs = VirtualCamera.get_max_devices()
    devices_str = ",".join(str(20 + i) for i in range(max_devs))
    exclusive_caps_str = ",".join(["1"] * max_devs)
    labels_str = ",".join([f"BigCam Virtual {i+1}" for i in range(max_devs)])
    return [
        _modprobe,
        "v4l2loopback",
        f"devices={max_devs}",
        f"exclusive_caps={exclusive_caps_str}",
        "max_buffers=8",
        f"video_nr={devices_str}",
        f"card_label={labels_str}",
    ]


class VirtualCamera:
    """Manage v4l2loopback virtual camera output.

    Supports multiple simultaneous virtual cameras — one per physical camera.
    Devices are created dynamically via v4l2loopback-ctl when available,
    falling back to a fixed pool of 4 devices otherwise.
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
        """Check if v4l2loopback-ctl is available for dynamic device management."""
        if cls._dynamic_supported is None:
            cls._dynamic_supported = os.path.isfile(_V4L2LOOPBACK_CTL)
        return cls._dynamic_supported

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
        """Return a v4l2loopback device not currently allocated to any camera."""
        with cls._alloc_lock:
            allocated = set(cls._allocations.values())
        devices = cls.find_all_loopback_devices()
        if not cls._is_dynamic_supported():
            # If no v4l2loopback-ctl, search in the allocated pool range
            for i in range(cls._max_devices):
                dev = f"/dev/video{20 + i}"
                if dev not in allocated and os.path.exists(dev):
                    return dev
        for dev in devices:
            if dev not in allocated:
                return dev
        return ""

    @classmethod
    def _add_dynamic_device(cls, label: str) -> str:
        """Dynamically create a v4l2loopback device via v4l2loopback-ctl.

        Devices start at /dev/video20 to avoid conflicts with physical cameras.
        """
        # Find the next available high device number (20+)
        dev_num = 20
        with cls._alloc_lock:
            used_nums = set()
            for dev in list(cls._allocations.values()) + list(cls._dynamic_devices):
                try:
                    used_nums.add(int(dev.replace("/dev/video", "")))
                except (ValueError, AttributeError):
                    pass
        while dev_num in used_nums or os.path.exists(f"/dev/video{dev_num}"):
            dev_num += 1
        try:
            result = SecureCommandRunner.run_safe(
                ["sudo", "-n", _V4L2LOOPBACK_CTL, "add",
                 "-n", label, "-x", "1", "-b", "8",
                 f"/dev/video{dev_num}"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                dev = result.stdout.strip()
                if dev.startswith("/dev/video"):
                    with cls._alloc_lock:
                        cls._dynamic_devices.add(dev)
                    log.info("Dynamically created v4l2loopback: %s (%s)", dev, label)
                    return dev
            log.warning(
                "v4l2loopback-ctl add failed (rc=%d): %s",
                result.returncode,
                result.stderr.strip(),
            )
        except Exception:
            log.error("Failed to run v4l2loopback-ctl add", exc_info=True)
        return ""

    @classmethod
    def _delete_dynamic_device(cls, dev: str) -> bool:
        """Delete a dynamically created v4l2loopback device."""
        try:
            result = SecureCommandRunner.run_safe(
                ["sudo", "-n", _V4L2LOOPBACK_CTL, "delete", dev],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode == 0:
                with cls._alloc_lock:
                    cls._dynamic_devices.discard(dev)
                log.info("Deleted v4l2loopback device: %s", dev)
                return True
            log.warning("v4l2loopback-ctl delete failed for %s: %s",
                        dev, result.stderr.strip())
        except Exception:
            log.error("Failed to delete v4l2loopback device %s", dev, exc_info=True)
        return False

    @classmethod
    def allocate_device(cls, camera_id: str) -> str:
        """Allocate a v4l2loopback device for a camera. Returns device path."""
        with cls._alloc_lock:
            # Already allocated?
            if camera_id in cls._allocations:
                return cls._allocations[camera_id]
            # Check max devices limit
            if len(cls._allocations) >= cls._max_devices:
                log.warning("Max virtual cameras (%d) reached, cannot allocate for %s",
                            cls._max_devices, camera_id)
                from core.event_bus import event_bus
                event_bus.emit("vcam-limit-reached", cls._max_devices)
                return ""
            device = ""
            if cls._is_dynamic_supported():
                # Prefer a free device whose label matches the template
                dev_labels = cls._get_device_labels()
                tpl_pat = re.compile(
                    re.escape(cls._name_template) + r"\s+\d+$"
                )
                allocated = set(cls._allocations.values())
                for dev in cls.find_all_loopback_devices():
                    if dev not in allocated:
                        lbl = dev_labels.get(dev, "")
                        if lbl and tpl_pat.match(lbl):
                            device = dev
                            break
                if not device:
                    # No matching free device — create a dynamic one
                    existing = cls._get_existing_labels()
                    n = 1
                    while f"{cls._name_template} {n}" in existing:
                        n += 1
                    label = f"{cls._name_template} {n}"
                    device = cls._add_dynamic_device(label)
            else:
                # Fallback: use any free loopback device (no dynamic support)
                device = cls.find_free_loopback_device()
            if device:
                cls._allocations[camera_id] = device
                log.info("Allocated %s for camera %s", device, camera_id)
            else:
                from core.event_bus import event_bus
                actual_limit = min(len(cls._allocations), cls._max_devices)
                event_bus.emit("vcam-limit-reached", actual_limit)
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
        """Delete all dynamically created v4l2loopback devices.

        Also removes stale devices from previous sessions that are no
        longer tracked by the app (prevents device accumulation).
        """
        with cls._alloc_lock:
            tracked = list(cls._dynamic_devices)
            cls._allocations.clear()
        deleted = 0
        for dev in tracked:
            if cls._delete_dynamic_device(dev):
                deleted += 1
        # Clean up stale v4l2loopback devices not tracked in this session
        if cls._is_dynamic_supported():
            all_loopback = cls.find_all_loopback_devices()
            stale = [d for d in all_loopback if d not in tracked]
            for dev in stale:
                if cls._delete_dynamic_device(dev):
                    deleted += 1
        with cls._alloc_lock:
            cls._dynamic_devices.clear()
            cls._next_vcam_number = 1
            cls._labels_synced = False
        log.info("Cleaned up %d v4l2loopback devices", deleted)

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

        When v4l2loopback-ctl is available, loads with devices=0 and
        creates devices dynamically. Otherwise falls back to 4 fixed devices.
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
        device = cls.find_loopback_device()
        return device

    @staticmethod
    def _reload_module() -> bool:
        """Unload and reload v4l2loopback with correct parameters."""
        _run_privileged("unload")
        return _run_privileged("load")


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
