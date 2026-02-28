"""Virtual camera page – v4l2loopback management."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, GLib, GObject

from core.virtual_camera import VirtualCamera
from utils.i18n import _


class VirtualCameraPage(Gtk.Box):
    """Page for managing the virtual camera (v4l2loopback) output."""

    __gsignals__ = {
        "virtual-camera-toggled": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
    }

    def __init__(self) -> None:
        super().__init__(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )

        clamp = Adw.Clamp(maximum_size=600, tightening_threshold=400)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        # Status group
        status_group = Adw.PreferencesGroup(title=_("Virtual Camera"))

        self._status_row = Adw.ActionRow(
            title=_("Status"),
        )
        self._status_icon = Gtk.Image.new_from_icon_name("emblem-default-symbolic")
        self._status_row.add_prefix(self._status_icon)
        status_group.add(self._status_row)

        self._device_row = Adw.ActionRow(
            title=_("Device"),
            subtitle=_("Not loaded"),
        )
        status_group.add(self._device_row)

        content.append(status_group)

        # Actions group
        actions_group = Adw.PreferencesGroup(title=_("Actions"))

        self._toggle_row = Adw.SwitchRow(
            title=_("Enable virtual camera"),
            subtitle=_("Create a virtual camera output for video calls and streaming."),
        )
        self._toggle_row.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Enable virtual camera")]
        )
        self._toggle_row.connect("notify::active", self._on_toggle)
        actions_group.add(self._toggle_row)

        content.append(actions_group)

        # Info group
        info_group = Adw.PreferencesGroup(
            title=_("Usage"),
            description=_(
                "When enabled, the active camera preview is sent to a virtual camera "
                "device that applications like OBS Studio, Google Meet, and Zoom can use."
            ),
        )
        content.append(info_group)

        clamp.set_child(content)
        self.append(clamp)

        self._updating_ui = False
        self._refresh_status()

    def _refresh_status(self) -> None:
        self._updating_ui = True
        try:
            if not VirtualCamera.is_available():
                self._status_row.set_subtitle(_("v4l2loopback not available"))
                self._status_icon.set_from_icon_name("dialog-warning-symbolic")
                self._toggle_row.set_sensitive(False)
                return

            device = VirtualCamera.find_loopback_device()
            enabled = VirtualCamera.is_enabled()

            if enabled and device:
                self._status_row.set_subtitle(_("Active"))
                self._status_icon.set_from_icon_name("emblem-ok-symbolic")
                self._device_row.set_subtitle(device)
                self._toggle_row.set_active(True)
            elif device:
                self._status_row.set_subtitle(_("Module loaded"))
                self._status_icon.set_from_icon_name("emblem-default-symbolic")
                self._device_row.set_subtitle(device)
                self._toggle_row.set_active(False)
            else:
                self._status_row.set_subtitle(_("Module not loaded"))
                self._status_icon.set_from_icon_name("dialog-information-symbolic")
                self._device_row.set_subtitle(_("Not loaded"))
                self._toggle_row.set_active(False)
        finally:
            self._updating_ui = False

    def _on_toggle(self, row: Adw.SwitchRow, _pspec) -> None:
        if self._updating_ui:
            return
        active = row.get_active()
        VirtualCamera.set_enabled(active)
        self.emit("virtual-camera-toggled", active)
        GLib.timeout_add(500, self._refresh_status_once)

    def _refresh_status_once(self) -> bool:
        self._refresh_status()
        return False

    def set_toggle_active(self, active: bool) -> None:
        """Set toggle state without emitting the toggled signal."""
        self._updating_ui = True
        self._toggle_row.set_active(active)
        self._updating_ui = False
        self._refresh_status()
