"""Global constants for Big DigiCam."""

import enum
import os

APP_ID = "br.com.biglinux.digicam"
APP_NAME = "Big DigiCam"
APP_VERSION = "2.0.0"
APP_ICON = "big-digicam"
APP_WEBSITE = "https://github.com/biglinux/big-digicam"
APP_ISSUE_URL = "https://github.com/biglinux/big-digicam/issues"
APP_COPYRIGHT = "\u00a9 2026 BigLinux Team"

BASE_DIR = os.path.dirname(os.path.realpath(__file__))


class BackendType(enum.Enum):
    V4L2 = "v4l2"
    GPHOTO2 = "gphoto2"
    LIBCAMERA = "libcamera"
    PIPEWIRE = "pipewire"
    IP = "ip"


class ControlCategory(enum.Enum):
    IMAGE = "image"
    EXPOSURE = "exposure"
    FOCUS = "focus"
    WHITE_BALANCE = "wb"
    ADVANCED = "advanced"


class ControlType(enum.Enum):
    INTEGER = "int"
    BOOLEAN = "bool"
    MENU = "menu"
    BUTTON = "button"
