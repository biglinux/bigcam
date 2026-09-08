"""gettext catalog shared with the browser client; no translated executable code."""
from utils.i18n import _


def phone_strings():
    return {
        "title": _("Phone as Webcam"), "disconnected": _("Disconnected"),
        "connecting": _("Connecting…"), "connected": _("Connected"),
        "start": _("Start"), "stop": _("Stop"), "switch": _("Switch camera"),
        "resolution": _("Resolution"), "camera": _("Camera"), "quality": _("Quality"),
        "fps": _("Frames per second"), "auto": _("Auto"), "back": _("Rear camera"),
        "front": _("Front camera"), "low": _("Low"), "medium": _("Medium"), "high": _("High"),
        "microphone": _("Include microphone audio"), "preview": _("Camera preview"),
        "tip": _("Only connect on a trusted network. Anyone with this address can connect to the camera service."),
        "error": _("Connection failed. Check permissions, the address and whether another device is connected."),
        "audioError": _("Microphone audio is unavailable. Stop and reconnect, or turn off microphone audio."),
        "authentication": _("This address is no longer valid. Scan the current QR code in BigCam."),
    }
