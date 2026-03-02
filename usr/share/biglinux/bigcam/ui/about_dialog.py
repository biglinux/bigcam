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


def show_about(parent: Gtk.Window) -> None:
    """Present the standard Adwaita about dialog."""
    dialog = Adw.AboutDialog.new()
    dialog.set_application_name(APP_NAME)
    dialog.set_version(APP_VERSION)
    dialog.set_application_icon(APP_ICON)
    dialog.set_developer_name("BigLinux Team")
    dialog.set_website(APP_WEBSITE)
    dialog.set_issue_url(APP_ISSUE_URL)
    dialog.set_copyright(APP_COPYRIGHT)
    dialog.set_license_type(Gtk.License.GPL_3_0)
    dialog.set_developers(["BigLinux Team"])
    dialog.set_comments(_("Universal webcam control center for Linux."))
    dialog.present(parent)
