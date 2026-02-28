"""Global constants for BigCam."""

import enum
import os

APP_ID = "br.com.biglinux.bigcam"
APP_NAME = "BigCam"
APP_VERSION = "2.0.0"
APP_ICON = "bigcam"
APP_WEBSITE = "https://github.com/biglinux/bigcam"
APP_ISSUE_URL = "https://github.com/biglinux/bigcam/issues"
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
    CAPTURE = "capture"
    STATUS = "status"
    ADVANCED = "advanced"


class ControlType(enum.Enum):
    INTEGER = "int"
    BOOLEAN = "bool"
    MENU = "menu"
    BUTTON = "button"
    STRING = "string"
