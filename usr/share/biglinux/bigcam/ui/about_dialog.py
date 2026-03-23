"""About Dialog – Opaque, no extra deps."""

from __future__ import annotations

import gi

gi.require_version("Adw", "1")
gi.require_version("Gtk", "4.0")

from gi.repository import Adw, Gtk

from constants import (
    APP_NAME,
    APP_VERSION,
    APP_ICON,
    APP_WEBSITE,
    APP_ISSUE_URL,
    APP_COPYRIGHT,
)
from utils.i18n import _


def create_about_dialog() -> Adw.AboutDialog:
    """Create the standard Adwaita about dialog (caller presents it)."""
    dialog = Adw.AboutDialog.new()
    dialog.set_application_name(APP_NAME)
    dialog.set_version(APP_VERSION)
    dialog.set_application_icon(APP_ICON)
    dialog.set_developer_name("BigLinux Team")
    dialog.set_website(APP_WEBSITE)
    dialog.set_issue_url(APP_ISSUE_URL)
    dialog.set_copyright(APP_COPYRIGHT)
    dialog.set_license_type(Gtk.License.GPL_3_0)
    dialog.set_developers([
        "Rafael Ruscher <rruscher@gmail.com>",
        "Barnabé di Kartola <barnabedikartola@gmail.com>",
    ])
    dialog.set_comments(_(
        "The universal webcam control center for Linux.\n\n"
        "BigCam was born as a small shell script so that Rafael Ruscher "
        "could use his Canon Rebel T3 as a webcam during live streams "
        "about BigLinux. That humble hack, written by Rafael and Barnabé "
        "di Kartola, evolved from a Bash bridge between gPhoto2 and FFmpeg "
        "into a full GTK4/Adwaita application with live preview, "
        "multi-backend camera support (V4L2, gPhoto2, libcamera, PipeWire, "
        "IP cameras, smartphones), real-time OpenCV effects, virtual camera "
        "output, photo and video capture, and 29 languages."
    ))
    return dialog


def show_about(parent: Gtk.Window) -> None:
    """Present the standard Adwaita about dialog."""
    create_about_dialog().present(parent)
