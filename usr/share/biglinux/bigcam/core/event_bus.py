"""Event Bus for decoupled communication between components."""

import gi
gi.require_version("GObject", "2.0")
from gi.repository import GObject

class EventBus(GObject.Object):
    """Central event broker for BigCam."""

    __gsignals__ = {
        "camera-changed": (GObject.SignalFlags.RUN_LAST, None, (object,)),
        "mobile-status-changed": (GObject.SignalFlags.RUN_LAST, None, (str, str)), # (backend_type, status)
        "error": (GObject.SignalFlags.RUN_LAST, None, (str, str)), # (source, message)
        "qr-detected": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "sidebar-toggled": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
        "vcam-limit-reached": (GObject.SignalFlags.RUN_LAST, None, (int,)),
    }

    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(EventBus, cls).__new__(cls, *args, **kwargs)
        return cls._instance

event_bus = EventBus()
