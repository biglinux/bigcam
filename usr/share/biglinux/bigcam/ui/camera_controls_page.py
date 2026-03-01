"""Camera controls page – dynamic controls sidebar grouped by category."""

from __future__ import annotations

import threading
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, GLib, GObject

from constants import ControlCategory, ControlType
from core.camera_backend import CameraControl, CameraInfo
from core.camera_manager import CameraManager
from utils.i18n import _

_CATEGORY_LABELS = {
    ControlCategory.IMAGE: _("Image"),
    ControlCategory.EXPOSURE: _("Exposure"),
    ControlCategory.FOCUS: _("Focus"),
    ControlCategory.WHITE_BALANCE: _("White Balance"),
    ControlCategory.CAPTURE: _("Capture"),
    ControlCategory.STATUS: _("Status"),
    ControlCategory.ADVANCED: _("Advanced"),
}

_CATEGORY_ICONS = {
    ControlCategory.IMAGE: "applications-graphics-symbolic",
    ControlCategory.EXPOSURE: "camera-photo-symbolic",
    ControlCategory.FOCUS: "find-location-symbolic",
    ControlCategory.WHITE_BALANCE: "weather-clear-symbolic",
    ControlCategory.CAPTURE: "media-record-symbolic",
    ControlCategory.STATUS: "dialog-information-symbolic",
    ControlCategory.ADVANCED: "emblem-system-symbolic",
}


class CameraControlsPage(Gtk.ScrolledWindow):
    """Dynamically-populated sidebar with all controls of the active camera."""

    def __init__(self, camera_manager: CameraManager) -> None:
        super().__init__(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        self._manager = camera_manager
        self._camera: CameraInfo | None = None
        self._controls: list[CameraControl] = []
        self._debounce_sources: dict[str, int] = {}
        self._ctrl_widgets: dict[str, tuple[str, Any]] = {}
        self._resetting = False

        self._clamp = Adw.Clamp(maximum_size=600, tightening_threshold=400)
        self._content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )
        self._clamp.set_child(self._content)
        self.set_child(self._clamp)

        # Status page when no camera
        self._empty = Adw.StatusPage(
            icon_name="camera-web-symbolic",
            title=_("No camera selected"),
            description=_("Select a camera to see its controls."),
        )
        self._content.append(self._empty)

    # -- public API ----------------------------------------------------------

    def set_camera_with_controls(
        self, camera: CameraInfo, controls: list[CameraControl],
    ) -> None:
        """Set camera and display pre-fetched controls (avoids USB conflict)."""
        self._camera = camera
        self._controls = controls
        self._clear_content()
        self._populate(controls)

    def set_camera(self, camera: CameraInfo | None) -> None:
        self._camera = camera
        self._clear_content()
        if camera is None:
            self._content.append(self._empty)
            return
        # Show loading spinner while fetching controls in background
        spinner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                              halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                              vexpand=True)
        spinner = Gtk.Spinner(spinning=True, width_request=32, height_request=32)
        spinner_box.append(spinner)
        spinner_box.append(Gtk.Label(label=_("Loading controls…")))
        self._content.append(spinner_box)

        def fetch_controls() -> list[CameraControl]:
            return self._manager.get_controls(camera)

        def on_controls(controls: list[CameraControl]) -> None:
            # Ensure we're still on the same camera
            if self._camera is not camera:
                return
            self._controls = controls
            self._clear_content()
            self._populate(controls)

        threading.Thread(target=lambda: GLib.idle_add(on_controls, fetch_controls()),
                         daemon=True).start()

    def _clear_content(self) -> None:
        child = self._content.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self._content.remove(child)
            child = next_child
        self._debounce_sources.clear()
        self._ctrl_widgets.clear()

    # -- build UI from controls list -----------------------------------------

    def _populate(self, controls: list[CameraControl]) -> None:
        if not controls:
            from constants import BackendType
            if self._camera and self._camera.backend == BackendType.PHONE:
                empty = Adw.StatusPage(
                    icon_name="phone-symbolic",
                    title=_("Phone camera"),
                    description=_(
                        "Adjust resolution, quality, and FPS directly on the phone's browser page. "
                        "Use the Effects tab for brightness, contrast, and other adjustments."
                    ),
                )
            else:
                empty = Adw.StatusPage(
                    icon_name="emblem-important-symbolic",
                    title=_("No adjustable controls"),
                    description=_("This camera does not expose any controls."),
                )
            self._content.append(empty)
            return

        # Group by category
        groups: dict[ControlCategory, list[CameraControl]] = {}
        for ctrl in controls:
            groups.setdefault(ctrl.category, []).append(ctrl)

        order = [
            ControlCategory.IMAGE,
            ControlCategory.EXPOSURE,
            ControlCategory.FOCUS,
            ControlCategory.WHITE_BALANCE,
            ControlCategory.CAPTURE,
            ControlCategory.STATUS,
            ControlCategory.ADVANCED,
        ]

        for cat in order:
            ctrls = groups.get(cat)
            if not ctrls:
                continue
            group = Adw.PreferencesGroup(
                title=_CATEGORY_LABELS.get(cat, cat.value),
            )
            group.set_header_suffix(self._make_reset_button(cat, ctrls))
            for ctrl in ctrls:
                row = self._make_row(ctrl)
                if row:
                    group.add(row)
            self._content.append(group)

    # -- row builders --------------------------------------------------------

    def _make_row(self, ctrl: CameraControl) -> Gtk.Widget | None:
        readonly = "read-only" in (ctrl.flags or "")

        if ctrl.control_type == ControlType.BOOLEAN:
            row = Adw.SwitchRow(title=ctrl.name)
            row.set_active(bool(ctrl.value))
            row.set_sensitive(not readonly)
            row.update_property(
                [Gtk.AccessibleProperty.LABEL], [ctrl.name]
            )
            if not readonly:
                row.connect("notify::active", self._on_switch, ctrl)
            self._ctrl_widgets[ctrl.id] = ("bool", row)
            return row

        if ctrl.control_type == ControlType.MENU:
            row = Adw.ComboRow(title=ctrl.name)
            model = Gtk.StringList()
            for ch in (ctrl.choices or []):
                model.append(ch)
            row.set_model(model)
            row.set_sensitive(not readonly)
            row.update_property(
                [Gtk.AccessibleProperty.LABEL], [ctrl.name]
            )
            # Select current (value is a numeric v4l2 index)
            if isinstance(ctrl.value, int) and ctrl.choices:
                idx = ctrl.value - (ctrl.minimum or 0)
                if 0 <= idx < len(ctrl.choices):
                    row.set_selected(idx)
            if not readonly:
                row.connect("notify::selected", self._on_combo, ctrl)
            self._ctrl_widgets[ctrl.id] = ("menu", row)
            return row

        if ctrl.control_type == ControlType.INTEGER:
            row = Adw.ActionRow(title=ctrl.name)
            row.update_property(
                [Gtk.AccessibleProperty.LABEL], [ctrl.name]
            )
            adj = Gtk.Adjustment(
                value=float(ctrl.value or 0),
                lower=float(ctrl.minimum or 0),
                upper=float(ctrl.maximum or 100),
                step_increment=float(ctrl.step or 1),
            )
            scale = Gtk.Scale(
                orientation=Gtk.Orientation.HORIZONTAL,
                adjustment=adj,
                hexpand=True,
                draw_value=True,
                value_pos=Gtk.PositionType.LEFT,
            )
            scale.set_size_request(180, -1)
            scale.set_sensitive(not readonly)
            if not readonly:
                adj.connect("value-changed", self._on_scale_debounced, ctrl)
            row.add_suffix(scale)
            self._ctrl_widgets[ctrl.id] = ("int", adj)
            return row

        if ctrl.control_type == ControlType.STRING:
            if readonly:
                row = Adw.ActionRow(title=ctrl.name)
                row.add_suffix(
                    Gtk.Label(
                        label=str(ctrl.value or ""),
                        selectable=True,
                        css_classes=["dim-label"],
                    )
                )
            else:
                row = Adw.EntryRow(title=ctrl.name)
                row.set_text(str(ctrl.value or ""))
                row.connect("apply", self._on_entry_apply, ctrl)
            row.update_property(
                [Gtk.AccessibleProperty.LABEL], [ctrl.name]
            )
            row.set_sensitive(True)
            return row

        return None

    # -- signal handlers -----------------------------------------------------

    def _on_switch(self, row: Adw.SwitchRow, _pspec: Any, ctrl: CameraControl) -> None:
        if self._resetting:
            return
        val = 1 if row.get_active() else 0
        self._apply(ctrl, val)

    def _on_combo(self, row: Adw.ComboRow, _pspec: Any, ctrl: CameraControl) -> None:
        if self._resetting:
            return
        idx = row.get_selected()
        choices = ctrl.choices or []
        if 0 <= idx < len(choices):
            self._apply(ctrl, idx + (ctrl.minimum or 0))

    def _on_scale_debounced(self, adj: Gtk.Adjustment, ctrl: CameraControl) -> None:
        if self._resetting:
            return
        # Debounce 50 ms
        if ctrl.id in self._debounce_sources:
            GLib.source_remove(self._debounce_sources[ctrl.id])
        self._debounce_sources[ctrl.id] = GLib.timeout_add(
            50, self._apply_scale, adj, ctrl
        )

    def _apply_scale(self, adj: Gtk.Adjustment, ctrl: CameraControl) -> bool:
        self._debounce_sources.pop(ctrl.id, None)
        self._apply(ctrl, int(adj.get_value()))
        return False

    def _apply(self, ctrl: CameraControl, value: Any) -> None:
        if self._camera:
            self._manager.set_control(self._camera, ctrl.id, value)

    def _on_entry_apply(self, row: Adw.EntryRow, ctrl: CameraControl) -> None:
        self._apply(ctrl, row.get_text())

    # -- reset button --------------------------------------------------------

    def _make_reset_button(self, cat: ControlCategory, ctrls: list[CameraControl]) -> Gtk.Button:
        btn = Gtk.Button.new_from_icon_name("edit-undo-symbolic")
        btn.add_css_class("flat")
        btn.set_tooltip_text(_("Reset to defaults"))
        btn.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Reset %s controls") % _CATEGORY_LABELS.get(cat, "")],
        )
        btn.connect("clicked", self._on_reset, ctrls)
        return btn

    def _on_reset(self, _btn: Gtk.Button, ctrls: list[CameraControl]) -> None:
        if self._camera:
            self._resetting = True
            self._manager.reset_all_controls(self._camera, ctrls)
            for ctrl in ctrls:
                ctrl.value = ctrl.default
                entry = self._ctrl_widgets.get(ctrl.id)
                if not entry:
                    continue
                kind, widget = entry
                if kind == "bool":
                    widget.set_active(bool(ctrl.default))
                elif kind == "menu":
                    if isinstance(ctrl.default, int) and ctrl.choices:
                        idx = ctrl.default - (ctrl.minimum or 0)
                        if 0 <= idx < len(ctrl.choices):
                            widget.set_selected(idx)
                elif kind == "int":
                    widget.set_value(float(ctrl.default or 0))
            self._resetting = False
