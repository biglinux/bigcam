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

# Single source of truth for the device pool.  Must match
# script/bigcam-v4l2loopback (DEVICE_BASE/MAX_DEVICES) and
# etc/modprobe.d/v4l2loopback.conf, otherwise the fallback path looks for
# devices at numbers the module never created.
DEVICE_BASE = 20
DEVICE_POOL_SIZE = 8

# The single privileged entry point.  It validates its own arguments; see
# etc/sudoers.d/bigcam for why we no longer call modprobe directly.
_HELPER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
    "script",
    "bigcam-v4l2loopback",
)


def _run_privileged(*args: str) -> subprocess.CompletedProcess:
    """Run the BigCam v4l2loopback helper with elevated privileges.

    Tries passwordless sudo first (etc/sudoers.d/bigcam, wheel group).  If
    sudo refuses — user not in wheel, or the sudoers drop-in is not installed
    — falls back to pkexec, which prompts via the polkit agent.

    Returns the CompletedProcess so callers can read stdout (``add`` prints
    the created device path).  Never raises.
    """
    attempts: list[list[str]] = [["sudo", "-n", _HELPER, *args]]
    pkexec = shutil.which("pkexec")
    if pkexec:
        attempts.append([pkexec, _HELPER, *args])

    last = subprocess.CompletedProcess(list(args), 1, stdout="", stderr="")
    for cmd in attempts:
        try:
            result = SecureCommandRunner.run_safe(
                cmd, capture_output=True, text=True, timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.debug("%s failed to run: %s", cmd[0], exc)
            last = subprocess.CompletedProcess(cmd, 1, stdout="", stderr=str(exc))
            continue
        if result.returncode == 0:
            return result
        log.debug(
            "%s %s failed (rc=%d): %s",
            cmd[0], " ".join(args), result.returncode, (result.stderr or "").strip(),
        )
        last = result
    log.error(
        "v4l2loopback helper '%s' failed (rc=%d): %s",
        " ".join(args), last.returncode, (last.stderr or "").strip(),
    )
    return last


class VirtualCamera:
    """Manage v4l2loopback virtual camera output.

    Supports multiple simultaneous virtual cameras — one per physical camera.
    Devices are created dynamically via v4l2loopback-ctl when available,
    falling back to a fixed pool of 4 devices otherwise.
    """

    _load_attempted: bool = False
    _enabled: bool = False
    _dynamic_supported: bool | None = None  # lazy-checked
    _max_devices: int = 5
    _name_template: str = "BigCam Virtual"

    # camera_id → v4l2loopback device path
    _allocations: dict[str, str] = {}
    # Devices created dynamically by v4l2loopback-ctl (need explicit cleanup)
    _dynamic_devices: set[str] = set()
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
            for i in range(min(cls._max_devices, DEVICE_POOL_SIZE)):
                dev = f"/dev/video{DEVICE_BASE + i}"
                if dev not in allocated and os.path.exists(dev):
                    return dev
        for dev in devices:
            if dev not in allocated:
                return dev
        return ""

    @classmethod
    def _add_dynamic_device(cls, label: str) -> str:
        """Dynamically create a v4l2loopback device via v4l2loopback-ctl.

        Devices are numbered from DEVICE_BASE so they never collide with
        physical cameras.  The number must stay inside the pool, because the
        privileged helper rejects anything outside it (that bound is what
        stops the helper from being aimed at a real camera node).
        """
        with cls._alloc_lock:
            used_nums = set()
            for dev in list(cls._allocations.values()) + list(cls._dynamic_devices):
                try:
                    used_nums.add(int(dev.replace("/dev/video", "")))
                except (ValueError, AttributeError):
                    pass

        dev_num = None
        for candidate in range(DEVICE_BASE, DEVICE_BASE + DEVICE_POOL_SIZE):
            if candidate in used_nums or os.path.exists(f"/dev/video{candidate}"):
                continue
            dev_num = candidate
            break
        if dev_num is None:
            log.warning(
                "No free slot in the v4l2loopback pool (/dev/video%d-%d)",
                DEVICE_BASE, DEVICE_BASE + DEVICE_POOL_SIZE - 1,
            )
            return ""
        try:
            result = _run_privileged("add", label, f"/dev/video{dev_num}")
            if result.returncode == 0:
                dev = (result.stdout or "").strip() or f"/dev/video{dev_num}"
                if dev.startswith("/dev/video"):
                    with cls._alloc_lock:
                        cls._dynamic_devices.add(dev)
                    log.info("Dynamically created v4l2loopback: %s (%s)", dev, label)
                    return dev
        except Exception:
            log.error("Failed to create v4l2loopback device", exc_info=True)
        return ""

    @classmethod
    def _delete_dynamic_device(cls, dev: str) -> bool:
        """Delete a dynamically created v4l2loopback device."""
        try:
            result = _run_privileged("delete", dev)
            if result.returncode == 0:
                with cls._alloc_lock:
                    cls._dynamic_devices.discard(dev)
                log.info("Deleted v4l2loopback device: %s", dev)
                return True
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
        """Release the device allocated to a camera, destroying it if we made it.

        Devices BigCam created dynamically are deleted right away; without
        this they pile up in /dev until the app exits.  Devices that already
        existed (static pool, or created by someone else) are only unbound.
        """
        with cls._alloc_lock:
            dev = cls._allocations.pop(camera_id, None)
            is_ours = dev in cls._dynamic_devices if dev else False
            still_used = dev in cls._allocations.values() if dev else False
        if not dev:
            return
        if is_ours and not still_used:
            cls._delete_dynamic_device(dev)
        log.debug("Released %s from camera %s (deleted=%s)",
                  dev, camera_id, is_ours and not still_used)

    @classmethod
    def get_device_for_camera(cls, camera_id: str) -> str:
        """Return the allocated device for a camera, or empty string."""
        with cls._alloc_lock:
            return cls._allocations.get(camera_id, "")

    @classmethod
    def _is_own_device(cls, device: str, labels: dict[str, str] | None = None) -> bool:
        """True when *device* carries a card label matching our name template.

        Used to make cleanup non-destructive: other applications (OBS,
        Droidcam, ...) create v4l2loopback devices too, and deleting theirs
        breaks their running streams.
        """
        if device in cls._dynamic_devices:
            return True
        if labels is None:
            labels = cls._get_device_labels()
        label = labels.get(device, "")
        if not label:
            return False
        pattern = re.compile(re.escape(cls._name_template) + r"(\s+\d+)?$")
        return bool(pattern.match(label))

    @classmethod
    def cleanup_dynamic_devices(cls) -> None:
        """Delete the v4l2loopback devices BigCam owns.

        Includes stale devices left behind by a previous BigCam session
        (identified by their card label), but never touches devices belonging
        to other applications.
        """
        with cls._alloc_lock:
            tracked = list(cls._dynamic_devices)
            cls._allocations.clear()
        deleted = 0
        for dev in tracked:
            if cls._delete_dynamic_device(dev):
                deleted += 1

        if cls._is_dynamic_supported():
            labels = cls._get_device_labels()
            stale = [
                d for d in cls.find_all_loopback_devices()
                if d not in tracked and cls._is_own_device(d, labels)
            ]
            for dev in stale:
                if cls._delete_dynamic_device(dev):
                    deleted += 1

        with cls._alloc_lock:
            cls._dynamic_devices.clear()
            cls._labels_synced = False
        log.info("Cleaned up %d BigCam v4l2loopback devices", deleted)

    @classmethod
    def reset_all_allocations(cls) -> None:
        """Delete dynamic devices and clear allocations (name template change).

        After calling this, the next allocate_device() calls will create
        new devices with the current name template.
        """
        cls.cleanup_dynamic_devices()

    @classmethod
    def load_module(cls) -> bool:
        """Load the v4l2loopback kernel module via the privileged helper."""
        return _run_privileged("load").returncode == 0

    @classmethod
    def set_enabled(cls, enabled: bool) -> None:
        cls._enabled = enabled

    @classmethod
    def is_enabled(cls) -> bool:
        return cls._enabled

    @classmethod
    def set_max_devices(cls, n: int) -> None:
        cls._max_devices = min(max(1, n), DEVICE_POOL_SIZE)

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

    @classmethod
    def get_name_template(cls) -> str:
        return cls._name_template

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
                cls.load_module()
                module_loaded = _is_module_loaded()

        if not module_loaded:
            return ""

        # Module is loaded — allocate a device (creates dynamically if needed)
        if camera_id:
            return cls.allocate_device(camera_id)
        device = cls.find_loopback_device()
        return device



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

