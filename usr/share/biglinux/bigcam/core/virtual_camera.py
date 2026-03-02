"""Virtual camera – v4l2loopback output for OBS / videoconference apps."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess

log = logging.getLogger(__name__)


class VirtualCamera:
    """Manage v4l2loopback virtual camera output."""

    _loopback_device: str = ""
    _process: subprocess.Popen | None = None
    _load_attempted: bool = False
    _enabled: bool = False

    @staticmethod
    def is_available() -> bool:
        return os.path.exists("/usr/lib/modules") and _has_v4l2loopback()

    @staticmethod
    def find_loopback_device() -> str:
        """Return /dev/video10 if it exists as a v4l2loopback device."""
        try:
            result = subprocess.run(
                ["v4l2-ctl", "-d", "/dev/video10", "--info"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and "v4l2 loopback" in result.stdout.lower():
                return "/dev/video10"
        except Exception:
            log.debug("Ignored exception", exc_info=True)
        # Fallback: scan all devices
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--list-devices"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for line in result.stdout.splitlines():
                if "v4l2loopback" in line.lower() or "virtual" in line.lower():
                    idx = result.stdout.splitlines().index(line) + 1
                    while idx < len(result.stdout.splitlines()):
                        dev = result.stdout.splitlines()[idx].strip()
                        if dev.startswith("/dev/video"):
                            return dev
                        idx += 1
        except Exception:
            log.debug("Ignored exception", exc_info=True)
        return ""

    @classmethod
    def load_module(cls, card_label: str | None = None) -> bool:
        """Load v4l2loopback kernel module with 2 devices.

        Device 10: BigCam virtual camera output (for sharing)
        Device 11: Reserved for gPhoto2 streaming
        """
        label = "BigCam Virtual"
        safe_label = label.replace('"', "").replace("\\", "")
        try:
            subprocess.run(
                [
                    "pkexec",
                    "modprobe",
                    "v4l2loopback",
                    "devices=2",
                    "exclusive_caps=1",
                    "video_nr=10,11",
                    f'card_label="{safe_label}","{safe_label} (v4l2)"',
                ],
                capture_output=True,
                check=True,
                timeout=30,
            )
            return True
        except Exception:
            return False

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
                    *shlex.split(gst_pipeline),
                    "!",
                    "v4l2sink",
                    f"device={device}",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
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
    def ensure_ready(cls, card_label: str | None = None) -> str:
        """Ensure v4l2loopback is loaded and return the device path.

        Only activates when virtual camera is enabled by the user.
        Tries to load the module once per session if not already loaded.
        Returns empty string if unavailable or not enabled.
        """
        if not cls._enabled:
            return ""
        device = cls.find_loopback_device()
        if device and _has_exclusive_caps():
            return device
        # Module loaded without exclusive_caps or not loaded at all
        if cls._load_attempted or not cls.is_available():
            return device  # return whatever we have
        cls._load_attempted = True
        # Reload module with correct parameters
        if device:
            cls._reload_module()
        else:
            cls.load_module(card_label=card_label)
        return cls.find_loopback_device()

    @staticmethod
    def _reload_module() -> bool:
        """Unload and reload v4l2loopback with correct parameters."""
        try:
            subprocess.run(
                ["pkexec", "modprobe", "-r", "v4l2loopback"],
                capture_output=True,
                check=True,
                timeout=30,
            )
        except Exception:
            return False
        return VirtualCamera.load_module()


def _has_v4l2loopback() -> bool:
    try:
        result = subprocess.run(
            ["modinfo", "v4l2loopback"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def _has_exclusive_caps() -> bool:
    """Check if the loaded v4l2loopback module has exclusive_caps enabled."""
    try:
        with open("/sys/module/v4l2loopback/parameters/exclusive_caps") as f:
            # Format: "Y,Y,..." or "N,N,..."
            return "Y" in f.read()
    except (FileNotFoundError, OSError):
        return False
