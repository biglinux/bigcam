#!/usr/bin/env python3
"""Big DigiCam – Universal webcam control center for Linux."""

from __future__ import annotations

import sys
import os
import signal

# Ensure the package root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")

from gi.repository import Adw, Gio, GLib, Gst

from constants import APP_ID, APP_NAME, APP_ICON
from ui.window import BigDigicamWindow

Gst.init(None)


class BigDigicamApp(Adw.Application):
    """Single-instance GTK4/Adwaita application."""

    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )

    def do_activate(self) -> None:
        win = self.get_active_window()
        if win is None:
            win = BigDigicamWindow(self)
        win.present()

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)

        # Load CSS
        from gi.repository import Gtk, Gdk
        css_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "style.css")
        if os.path.isfile(css_path):
            provider = Gtk.CssProvider()
            provider.load_from_path(css_path)
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

        # Quit action
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        self.set_accels_for_action("app.quit", ["<Primary>q"])


def main() -> int:
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    app = BigDigicamApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
