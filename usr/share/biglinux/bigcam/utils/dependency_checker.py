"""Check availability of system dependencies at runtime."""

import shutil
import subprocess


def _cmd_exists(name: str) -> bool:
    return shutil.which(name) is not None


def _module_importable(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False


def _kmod_loaded(name: str) -> bool:
    try:
        result = subprocess.run(
            ["lsmod"],
            capture_output=True,
            text=True,
        )
        return name in result.stdout
    except Exception:
        return False


def check_all() -> dict[str, bool]:
    """Return dict mapping dependency name → available bool."""
    return {
        "gphoto2": _cmd_exists("gphoto2"),
        "ffmpeg": _cmd_exists("ffmpeg"),
        "v4l2-ctl": _cmd_exists("v4l2-ctl"),
        "libcamera": _cmd_exists("cam") or _cmd_exists("libcamera-hello"),
        "pipewire": _cmd_exists("pw-cli"),
        "gstreamer": _module_importable("gi.repository.Gst"),
        "gtk4": _module_importable("gi.repository.Gtk"),
        "adwaita": _module_importable("gi.repository.Adw"),
        "v4l2loopback": _kmod_loaded("v4l2loopback"),
    }


def missing() -> list[str]:
    """Return list of missing critical dependencies."""
    deps = check_all()
    critical = ["gstreamer", "gtk4", "adwaita", "v4l2-ctl"]
    return [d for d in critical if not deps.get(d, False)]
