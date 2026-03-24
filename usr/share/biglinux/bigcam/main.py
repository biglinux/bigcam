#!/usr/bin/env python3
"""BigCam – Universal webcam control center for Linux."""

from __future__ import annotations

import sys
import os
import atexit
import signal
import subprocess
import logging

# Configure logging early
logging.basicConfig(
    level=logging.DEBUG,
    format="%(name)s:%(levelname)s: %(message)s",
    filename="/tmp/bigcam_debug.log",
    filemode="w",
)
# Also log INFO+ to stderr so we can see critical messages in terminal
_console = logging.StreamHandler()
_console.setLevel(logging.INFO)
_console.setFormatter(logging.Formatter("%(name)s:%(levelname)s: %(message)s"))
logging.getLogger().addHandler(_console)

# Ensure the package root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")

from gi.repository import Adw, Gio, GLib, Gst

from constants import APP_ID, APP_NAME, APP_ICON
from ui.window import BigDigicamWindow
from ui.welcome_dialog import WelcomeDialog
from utils.settings_manager import SettingsManager

Gst.init(None)


class BigDigicamApp(Adw.Application):
    """Single-instance GTK4/Adwaita application."""

    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )
        self._first_activation = True
        self._settings_mgr = SettingsManager()

    def do_activate(self) -> None:
        # Reuse existing window (may be hidden after "Keep camera on")
        win = self.get_active_window()
        if win is None:
            windows = self.get_windows()
            if windows:
                win = windows[0]
            else:
                win = BigDigicamWindow(self)
        if getattr(win, "_background_mode", False):
            win._background_mode = False
            win.set_visible(True)
            self.release()
        win.present()

        if self._first_activation:
            self._first_activation = False
            if WelcomeDialog.should_show(self._settings_mgr):
                GLib.idle_add(self._show_welcome, win)

    def _show_welcome(self, win: Gtk.Window) -> bool:
        self._welcome_dialog = WelcomeDialog(win, self._settings_mgr)
        if hasattr(win, "_immersion") and self._welcome_dialog._dialog:
            win._immersion.present_dialog(self._welcome_dialog._dialog, win)
        else:
            self._welcome_dialog.present()
        return False

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)

        from gi.repository import Gtk, Gdk

        # Register local icon directories (prepend so they take priority)
        base_dir = os.path.dirname(os.path.realpath(__file__))
        icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        img_dir = os.path.join(base_dir, "img")
        icons_dir = os.path.join(base_dir, "icons")
        existing = list(icon_theme.get_search_path())
        prepend = [d for d in (img_dir, icons_dir) if os.path.isdir(d)]
        icon_theme.set_search_path(prepend + existing)

        # Also add the system icon path that contains bigcam.svg
        sys_icon_dir = os.path.join(
            os.path.dirname(base_dir),  # up from bigcam/ to biglinux/
            "..",
            "..",
            "icons",  # usr/share/icons
        )
        sys_icon_dir = os.path.realpath(sys_icon_dir)
        if (
            os.path.isdir(sys_icon_dir)
            and sys_icon_dir not in icon_theme.get_search_path()
        ):
            icon_theme.add_search_path(sys_icon_dir)

        Gtk.Window.set_default_icon_name(APP_ICON)

        # Load CSS
        css_path = os.path.join(
            os.path.dirname(os.path.realpath(__file__)), "style.css"
        )
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


def _kill_child_processes() -> None:
    """Last-resort cleanup: kill any uxplay/scrcpy/gst-launch spawned by BigCam."""
    for name in ("uxplay -n BigCam", "scrcpy", "gst-launch-1.0 -q fdsrc"):
        try:
            subprocess.run(
                ["pkill", "-9", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
        except Exception:
            pass


def main() -> int:
    atexit.register(_kill_child_processes)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    GLib.set_prgname(APP_ID)
    GLib.set_application_name(APP_NAME)
    app = BigDigicamApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
