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
            margin_top=0,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )

        clamp = Adw.Clamp(maximum_size=600, tightening_threshold=400)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        # Status group with ExpanderRow
        status_group = Adw.PreferencesGroup(title=_("Virtual Camera"))

        self._status_expander = Adw.ExpanderRow(
            title=_("Status"),
        )
        self._status_dot = Gtk.DrawingArea()
        self._status_dot.set_content_width(12)
        self._status_dot.set_content_height(12)
        self._status_dot.set_valign(Gtk.Align.CENTER)
        self._dot_color = (0.6, 0.6, 0.6)  # gray default
        self._status_dot.set_draw_func(self._draw_dot)
        self._status_dot.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Virtual camera status")],
        )
        self._status_expander.add_prefix(self._status_dot)

        # Sub-rows inside expander
        self._device_row = Adw.ActionRow(
            title=_("Device"),
            subtitle=_("Not loaded"),
        )
        self._device_row.add_prefix(
            Gtk.Image.new_from_icon_name("drive-harddisk-symbolic")
        )
        self._status_expander.add_row(self._device_row)

        self._module_row = Adw.ActionRow(
            title=_("Module"),
            subtitle=_("v4l2loopback"),
        )
        self._module_row.add_prefix(
            Gtk.Image.new_from_icon_name("application-x-firmware-symbolic")
        )
        self._status_expander.add_row(self._module_row)

        status_group.add(self._status_expander)
        content.append(status_group)

        # Actions group
        actions_group = Adw.PreferencesGroup(title=_("Actions"))

        self._toggle_row = Adw.SwitchRow(
            title=_("Enable virtual camera"),
            subtitle=_("Create a virtual camera output for video calls and streaming."),
        )
        self._toggle_row.add_prefix(
            Gtk.Image.new_from_icon_name("camera-video-symbolic")
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

    def _draw_dot(self, area: Gtk.DrawingArea, cr, width: int, height: int) -> None:
        r, g, b = self._dot_color
        cr.set_source_rgb(r, g, b)
        cx, cy = width / 2, height / 2
        radius = min(width, height) / 2
        cr.arc(cx, cy, radius, 0, 2 * 3.14159)
        cr.fill()

    def _set_dot_color(self, r: float, g: float, b: float) -> None:
        self._dot_color = (r, g, b)
        self._status_dot.queue_draw()

    def _refresh_status(self) -> None:
        self._updating_ui = True
        try:
            if not VirtualCamera.is_available():
                self._status_expander.set_subtitle(_("v4l2loopback not available"))
                self._set_dot_color(0.85, 0.2, 0.2)  # red
                self._module_row.set_subtitle(_("Not installed"))
                self._device_row.set_subtitle(_("—"))
                self._toggle_row.set_sensitive(False)
                return

            device = VirtualCamera.find_loopback_device()
            enabled = VirtualCamera.is_enabled()

            if enabled and device:
                self._status_expander.set_subtitle(_("Active"))
                self._set_dot_color(0.2, 0.78, 0.35)  # green
                self._device_row.set_subtitle(device)
                self._module_row.set_subtitle(_("Loaded"))
                self._toggle_row.set_active(True)
            elif device:
                self._status_expander.set_subtitle(_("Module loaded"))
                self._set_dot_color(0.6, 0.6, 0.6)  # gray
                self._device_row.set_subtitle(device)
                self._module_row.set_subtitle(_("Loaded"))
                self._toggle_row.set_active(False)
            else:
                self._status_expander.set_subtitle(_("Module not loaded"))
                self._set_dot_color(0.6, 0.6, 0.6)  # gray
                self._device_row.set_subtitle(_("Not loaded"))
                self._module_row.set_subtitle(_("Not loaded"))
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
