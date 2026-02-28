"""Settings page – global application preferences."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, GLib, GObject

from utils.settings_manager import SettingsManager
from utils import xdg
from utils.i18n import _


class SettingsPage(Gtk.ScrolledWindow):
    """Application-wide preferences using Adw.PreferencesGroup widgets."""

    __gsignals__ = {
        "show-fps-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
        "mirror-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
    }"

    def __init__(self, settings: SettingsManager) -> None:
        super().__init__(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        self._settings = settings

        clamp = Adw.Clamp(maximum_size=600, tightening_threshold=400)
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )

        # -- General ---------------------------------------------------------
        general = Adw.PreferencesGroup(title=_("General"))

        # Photo directory
        photo_row = Adw.ActionRow(
            title=_("Photo directory"),
            subtitle=xdg.photos_dir(),
        )
        photo_row.set_activatable(False)
        general.add(photo_row)

        # Theme
        theme_row = Adw.ComboRow(title=_("Theme"))
        theme_model = Gtk.StringList()
        for t in (_("System"), _("Light"), _("Dark")):
            theme_model.append(t)
        theme_row.set_model(theme_model)
        theme_idx = {"system": 0, "light": 1, "dark": 2}.get(
            self._settings.get("theme"), 0
        )
        theme_row.set_selected(theme_idx)
        theme_row.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Application theme")]
        )
        theme_row.connect("notify::selected", self._on_theme)
        general.add(theme_row)

        content.append(general)

        # -- Preview ---------------------------------------------------------
        preview = Adw.PreferencesGroup(title=_("Preview"))

        mirror_row = Adw.SwitchRow(
            title=_("Mirror preview"),
            subtitle=_("Flip the preview horizontally like a mirror."),
        )
        mirror_row.set_active(self._settings.get("mirror_preview"))
        mirror_row.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Mirror preview")]
        )
        mirror_row.connect("notify::active", self._on_mirror)
        preview.add(mirror_row)

        show_fps_row = Adw.SwitchRow(
            title=_("Show FPS counter"),
        )
        show_fps_row.set_active(self._settings.get("show_fps"))
        show_fps_row.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Show FPS counter")]
        )
        show_fps_row.connect("notify::active", self._on_show_fps)
        preview.add(show_fps_row)

        content.append(preview)

        # -- Advanced --------------------------------------------------------
        advanced = Adw.PreferencesGroup(title=_("Advanced"))

        hotplug_row = Adw.SwitchRow(
            title=_("USB hotplug detection"),
            subtitle=_("Automatically detect cameras when plugged or unplugged."),
        )
        hotplug_row.set_active(self._settings.get("hotplug_enabled"))
        hotplug_row.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("USB hotplug detection")]
        )
        hotplug_row.connect("notify::active", self._on_hotplug)
        advanced.add(hotplug_row)

        content.append(advanced)

        clamp.set_child(content)
        self.set_child(clamp)

    # -- handlers ------------------------------------------------------------

    def _on_theme(self, row: Adw.ComboRow, _pspec) -> None:
        idx = row.get_selected()
        value = {0: "system", 1: "light", 2: "dark"}.get(idx, "system")
        self._settings.set("theme", value)
        style_manager = Adw.StyleManager.get_default()
        scheme_map = {
            "system": Adw.ColorScheme.DEFAULT,
            "light": Adw.ColorScheme.FORCE_LIGHT,
            "dark": Adw.ColorScheme.FORCE_DARK,
        }
        style_manager.set_color_scheme(scheme_map.get(value, Adw.ColorScheme.DEFAULT))

    def _on_mirror(self, row: Adw.SwitchRow, _pspec) -> None:
        active = row.get_active()
        self._settings.set("mirror_preview", active)
        self.emit("mirror-changed", active)

    def _on_show_fps(self, row: Adw.SwitchRow, _pspec) -> None:
        active = row.get_active()
        self._settings.set("show_fps", active)
        self.emit("show-fps-changed", active)

    def _on_hotplug(self, row: Adw.SwitchRow, _pspec) -> None:
        self._settings.set("hotplug_enabled", row.get_active())
