"""Virtual camera – v4l2loopback output for OBS / videoconference apps."""

from __future__ import annotations

import os
import re
import subprocess

from utils.i18n import _


class VirtualCamera:
    """Manage v4l2loopback virtual camera output."""

    _loopback_device: str = ""
    _process: subprocess.Popen | None = None

    @staticmethod
    def is_available() -> bool:
        return os.path.exists("/usr/lib/modules") and _has_v4l2loopback()

    @staticmethod
    def find_loopback_device() -> str:
        """Return /dev/videoN for the v4l2loopback device, creating it if needed."""
        try:
            result = subprocess.run(
                ["v4l2-ctl", "--list-devices"],
                capture_output=True,
                text=True,
            )
            for line in result.stdout.splitlines():
                if "v4l2loopback" in line.lower() or "virtual" in line.lower():
                    # Next line has the device path
                    idx = result.stdout.splitlines().index(line) + 1
                    while idx < len(result.stdout.splitlines()):
                        dev = result.stdout.splitlines()[idx].strip()
                        if dev.startswith("/dev/video"):
                            return dev
                        idx += 1
        except Exception:
            pass
        return ""

    @classmethod
    def load_module(cls) -> bool:
        """Load v4l2loopback kernel module."""
        try:
            subprocess.run(
                [
                    "pkexec", "modprobe", "v4l2loopback",
                    "devices=1", "exclusive_caps=1",
                    'video_nr=10', 'card_label="Big Digicam Virtual Camera"',
                ],
                capture_output=True,
                check=True,
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
                    *gst_pipeline.split(),
                    "!", "v4l2sink", f"device={device}",
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
    def is_running(cls) -> bool:
        return cls._process is not None and cls._process.poll() is None


def _has_v4l2loopback() -> bool:
    try:
        result = subprocess.run(
            ["modinfo", "v4l2loopback"],
            capture_output=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False
