"""Camera controls page – dynamic controls sidebar grouped by category."""

from __future__ import annotations

import threading
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, GLib

from constants import ControlCategory, ControlType
from core.camera_backend import CameraControl, CameraInfo
from core.camera_manager import CameraManager
from core import camera_profiles
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

    def __init__(self, camera_manager: CameraManager, stream_engine=None) -> None:
        super().__init__(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        self._manager = camera_manager
        self._engine = stream_engine
        self._camera: CameraInfo | None = None
        self._controls: list[CameraControl] = []
        self._debounce_sources: dict[str, int] = {}
        self._ctrl_widgets: dict[str, tuple[str, Any]] = {}
        self._ctrl_rows: dict[str, Gtk.Widget] = {}
        self._resetting = False

        # Auto-controls that disable their dependent manual controls.
        # Key = auto control ID, value = (dependent IDs, disable-when values).
        self._DEPENDENCIES: dict[str, tuple[list[str], set[int]]] = {
            "auto_exposure": (
                ["exposure_time_absolute", "exposure_absolute"],
                {3},  # 3 = Aperture Priority → disable manual exposure
            ),
            "white_balance_automatic": (
                ["white_balance_temperature"],
                {1},  # 1 = Auto WB on → disable temp
            ),
            "focus_auto": (
                ["focus_absolute"],
                {1},  # 1 = Auto focus on → disable manual focus
            ),
        }

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
        self,
        camera: CameraInfo,
        controls: list[CameraControl],
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
        spinner_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            vexpand=True,
        )
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

        threading.Thread(
            target=lambda: GLib.idle_add(on_controls, fetch_controls()), daemon=True
        ).start()

    def _clear_content(self) -> None:
        child = self._content.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self._content.remove(child)
            child = next_child
        self._debounce_sources.clear()
        self._ctrl_widgets.clear()
        self._ctrl_rows.clear()

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

        # Profiles section
        self._build_profiles_section()

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

        # Apply initial dependency state after all rows are created
        self._update_all_dependencies(controls)

    # -- row builders --------------------------------------------------------

    def _make_row(self, ctrl: CameraControl) -> Gtk.Widget | None:
        readonly = "read-only" in (ctrl.flags or "")

        if ctrl.control_type == ControlType.BOOLEAN:
            row = Adw.SwitchRow(title=ctrl.name)
            row.set_active(bool(ctrl.value))
            row.set_sensitive(not readonly)
            row.update_property([Gtk.AccessibleProperty.LABEL], [ctrl.name])
            if not readonly:
                row.connect("notify::active", self._on_switch, ctrl)
            self._ctrl_widgets[ctrl.id] = ("bool", row)
            self._ctrl_rows[ctrl.id] = row
            return row

        if ctrl.control_type == ControlType.MENU:
            row = Adw.ComboRow(title=ctrl.name)
            model = Gtk.StringList()
            for ch in ctrl.choices or []:
                model.append(ch)
            row.set_model(model)
            row.set_sensitive(not readonly)
            row.update_property([Gtk.AccessibleProperty.LABEL], [ctrl.name])
            # Select current using actual V4L2 indices
            if isinstance(ctrl.value, int) and ctrl.choice_values:
                try:
                    sel = ctrl.choice_values.index(ctrl.value)
                    row.set_selected(sel)
                except ValueError:
                    pass
            if not readonly:
                row.connect("notify::selected", self._on_combo, ctrl)
            self._ctrl_widgets[ctrl.id] = ("menu", row)
            self._ctrl_rows[ctrl.id] = row
            return row

        if ctrl.control_type == ControlType.INTEGER:
            row = Adw.ActionRow(title=ctrl.name)
            row.update_property([Gtk.AccessibleProperty.LABEL], [ctrl.name])
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
                draw_value=False,
            )
            scale.set_size_request(140, -1)
            scale.update_property(
                [
                    Gtk.AccessibleProperty.LABEL,
                    Gtk.AccessibleProperty.VALUE_NOW,
                    Gtk.AccessibleProperty.VALUE_MIN,
                    Gtk.AccessibleProperty.VALUE_MAX,
                ],
                [
                    ctrl.name,
                    float(adj.get_value()),
                    float(adj.get_lower()),
                    float(adj.get_upper()),
                ],
            )
            spin = Gtk.SpinButton(
                adjustment=adj,
                climb_rate=1.0,
                digits=0,
                width_chars=5,
                valign=Gtk.Align.CENTER,
            )
            spin.update_property([Gtk.AccessibleProperty.LABEL], [ctrl.name])
            scale.set_sensitive(not readonly)
            spin.set_sensitive(not readonly)
            if not readonly:
                adj.connect("value-changed", self._on_scale_debounced, ctrl)
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6, hexpand=True)
            box.append(scale)
            box.append(spin)
            row.add_suffix(box)
            self._ctrl_widgets[ctrl.id] = ("int", adj)
            self._ctrl_rows[ctrl.id] = row
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
            row.update_property([Gtk.AccessibleProperty.LABEL], [ctrl.name])
            row.set_sensitive(True)
            return row

        return None

    # -- profile management ----------------------------------------------------

    def _build_profiles_section(self) -> None:
        if not self._camera:
            return
        group = Adw.PreferencesGroup(title=_("Profiles"))

        # Profile selector
        profile_row = Adw.ComboRow(title=_("Profile"))
        profile_row.update_property([Gtk.AccessibleProperty.LABEL], [_("Profile")])
        self._profile_model = Gtk.StringList()
        self._profile_names: list[str] = []
        self._refresh_profile_list()
        profile_row.set_model(self._profile_model)
        profile_row.connect("notify::selected", self._on_profile_selected)
        self._profile_row = profile_row
        group.add(profile_row)

        # Action buttons row
        btn_row = Adw.ActionRow(title=_("Manage"))
        btn_row.update_property([Gtk.AccessibleProperty.LABEL], [_("Manage profiles")])

        save_btn = Gtk.Button(
            icon_name="document-save-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Save current settings as new profile"),
            css_classes=["flat"],
        )
        save_btn.update_property([Gtk.AccessibleProperty.LABEL], [_("Save profile")])
        save_btn.connect("clicked", self._on_save_profile)

        delete_btn = Gtk.Button(
            icon_name="user-trash-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Delete selected profile"),
            css_classes=["flat"],
        )
        delete_btn.update_property([Gtk.AccessibleProperty.LABEL], [_("Delete profile")])
        delete_btn.connect("clicked", self._on_delete_profile)
        self._delete_profile_btn = delete_btn

        hw_reset_btn = Gtk.Button(
            icon_name="view-refresh-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text=_("Reset all controls to hardware defaults"),
            css_classes=["flat"],
        )
        hw_reset_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Hardware defaults")]
        )
        hw_reset_btn.connect("clicked", self._on_hardware_reset)

        btn_row.add_suffix(save_btn)
        btn_row.add_suffix(delete_btn)
        btn_row.add_suffix(hw_reset_btn)
        group.add(btn_row)
        self._content.append(group)

    def _refresh_profile_list(self) -> None:
        if not self._camera:
            return
        self._profile_names = camera_profiles.list_profiles(self._camera)
        self._profile_model.splice(0, self._profile_model.get_n_items(), [])
        for name in self._profile_names:
            self._profile_model.append(name)

    def _on_profile_selected(self, row: Adw.ComboRow, _pspec: Any) -> None:
        if self._resetting or not self._camera:
            return
        idx = row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self._profile_names):
            return
        name = self._profile_names[idx]
        values = camera_profiles.load_profile(self._camera, name)
        if not values:
            return
        self._resetting = True
        for ctrl in self._controls:
            if ctrl.id in values:
                val = values[ctrl.id]
                ctrl.value = val
                entry = self._ctrl_widgets.get(ctrl.id)
                if entry:
                    kind, widget = entry
                    if kind == "bool":
                        widget.set_active(bool(val))
                    elif kind == "menu" and isinstance(val, int) and ctrl.choice_values:
                        try:
                            widget.set_selected(ctrl.choice_values.index(val))
                        except ValueError:
                            pass
                    elif kind == "int":
                        widget.set_value(float(val))
                threading.Thread(
                    target=lambda c=ctrl, v=val: self._manager.set_control(
                        self._camera, c.id, v
                    ),
                    daemon=True,
                ).start()
        self._resetting = False
        self._update_all_dependencies(self._controls)

    def _on_save_profile(self, _btn: Gtk.Button) -> None:
        if not self._camera:
            return
        dialog = Adw.MessageDialog(
            heading=_("Save Profile"),
            body=_("Enter a name for this profile:"),
            transient_for=self.get_root(),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("save", _("Save"))
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("save")

        entry = Gtk.Entry(
            placeholder_text=_("Profile name"),
            activates_default=True,
        )
        entry.update_property([Gtk.AccessibleProperty.LABEL], [_("Profile name")])
        dialog.set_extra_child(entry)

        def on_response(_dlg: Adw.MessageDialog, response: str) -> None:
            if response != "save":
                return
            name = entry.get_text().strip()
            if not name:
                return
            camera_profiles.save_profile(self._camera, name, self._controls)
            self._refresh_profile_list()
            if name in self._profile_names:
                self._profile_row.set_selected(self._profile_names.index(name))

        dialog.connect("response", on_response)
        dialog.present()

    def _on_delete_profile(self, _btn: Gtk.Button) -> None:
        if not self._camera:
            return
        idx = self._profile_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(self._profile_names):
            return
        name = self._profile_names[idx]
        camera_profiles.delete_profile(self._camera, name)
        self._refresh_profile_list()

    def _on_hardware_reset(self, _btn: Gtk.Button) -> None:
        """Reset all V4L2 controls to hardware default values."""
        if not self._camera or not self._controls:
            return
        import threading

        def _apply():
            self._manager.reset_all_controls(self._camera, self._controls)
            # Re-apply anti-flicker after reset (power_line_frequency defaults to 0)
            self._manager.apply_anti_flicker(self._camera)
            GLib.idle_add(self._reload_controls)

        threading.Thread(target=_apply, daemon=True).start()

    def _reload_controls(self) -> bool:
        """Refresh UI with current control values from hardware."""
        if not self._camera:
            return False
        controls = self._manager.get_controls(self._camera)
        if controls is None:
            return False
        self._controls = controls
        self._resetting = True
        for ctrl in controls:
            widget_info = self._ctrl_widgets.get(ctrl.id)
            if not widget_info:
                continue
            wtype, widget = widget_info
            if wtype == "int" and isinstance(widget, Gtk.Adjustment):
                widget.set_value(float(ctrl.value or 0))
            elif wtype == "bool" and isinstance(widget, Adw.SwitchRow):
                widget.set_active(bool(ctrl.value))
            elif wtype == "menu" and isinstance(widget, Adw.ComboRow):
                if isinstance(ctrl.value, int) and ctrl.choice_values:
                    try:
                        sel = ctrl.choice_values.index(ctrl.value)
                        widget.set_selected(sel)
                    except ValueError:
                        pass
        self._update_all_dependencies(controls)
        self._resetting = False
        return False

    # -- control dependencies -------------------------------------------------

    def _update_all_dependencies(self, controls: list[CameraControl]) -> None:
        """Apply initial sensitive state for dependent controls."""
        for ctrl in controls:
            if ctrl.id in self._DEPENDENCIES:
                dep_ids, disable_values = self._DEPENDENCIES[ctrl.id]
                disable = int(ctrl.value) in disable_values
                for dep_id in dep_ids:
                    row = self._ctrl_rows.get(dep_id)
                    if row:
                        row.set_sensitive(not disable)

    def _update_dependency(self, ctrl_id: str, value: int) -> None:
        """Update sensitive state of dependent controls after a change."""
        if ctrl_id not in self._DEPENDENCIES:
            return
        dep_ids, disable_values = self._DEPENDENCIES[ctrl_id]
        disable = value in disable_values
        for dep_id in dep_ids:
            row = self._ctrl_rows.get(dep_id)
            if row:
                row.set_sensitive(not disable)

    # -- signal handlers -----------------------------------------------------

    def _on_switch(self, row: Adw.SwitchRow, _pspec: Any, ctrl: CameraControl) -> None:
        if self._resetting:
            return
        val = 1 if row.get_active() else 0
        self._update_dependency(ctrl.id, val)
        self._apply(ctrl, val)

    def _on_combo(self, row: Adw.ComboRow, _pspec: Any, ctrl: CameraControl) -> None:
        if self._resetting:
            return
        idx = row.get_selected()
        val: int | None = None
        if ctrl.choice_values and 0 <= idx < len(ctrl.choice_values):
            val = ctrl.choice_values[idx]
        elif ctrl.choices and 0 <= idx < len(ctrl.choices):
            val = idx + (ctrl.minimum or 0)
        if val is not None:
            self._update_dependency(ctrl.id, val)
            self._apply(ctrl, val)

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
            # Run v4l2-ctl subprocess in background to avoid blocking UI
            camera = self._camera
            threading.Thread(
                target=lambda: self._manager.set_control(camera, ctrl.id, value),
                daemon=True,
            ).start()
            # Apply software zoom as fallback for cameras where V4L2 zoom is ineffective
            if ctrl.id == "zoom_absolute" and self._engine is not None:
                v4l_min = ctrl.minimum or 0
                v4l_max = ctrl.maximum or 10
                rng = max(v4l_max - v4l_min, 1)
                level = 1.0 + (int(value) - v4l_min) / rng * 3.0  # 1x-4x
                self._engine.set_zoom(level)
            # Apply software sharpness as fallback
            if ctrl.id == "sharpness" and self._engine is not None:
                v4l_min = ctrl.minimum or 0
                v4l_max = ctrl.maximum or 50
                rng = max(v4l_max - v4l_min, 1)
                level = (int(value) - v4l_min) / rng  # 0.0-1.0
                self._engine.set_sharpness(level)
            # Apply software pan as fallback
            if ctrl.id == "pan_absolute" and self._engine is not None:
                v4l_min = ctrl.minimum or -201600
                v4l_max = ctrl.maximum or 201600
                rng = max(v4l_max - v4l_min, 1)
                level = ((int(value) - v4l_min) / rng) * 2.0 - 1.0  # -1.0 to 1.0
                self._engine.set_pan(level)
            # Apply software tilt as fallback
            if ctrl.id == "tilt_absolute" and self._engine is not None:
                v4l_min = ctrl.minimum or -201600
                v4l_max = ctrl.maximum or 201600
                rng = max(v4l_max - v4l_min, 1)
                level = ((int(value) - v4l_min) / rng) * 2.0 - 1.0  # -1.0 to 1.0
                self._engine.set_tilt(level)

    def _on_entry_apply(self, row: Adw.EntryRow, ctrl: CameraControl) -> None:
        self._apply(ctrl, row.get_text())

    # -- reset button --------------------------------------------------------

    def _make_reset_button(
        self, cat: ControlCategory, ctrls: list[CameraControl]
    ) -> Gtk.Button:
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
            # Re-apply anti-flicker after reset (power_line_frequency defaults to 0)
            self._manager.apply_anti_flicker(self._camera)
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
                # Reset software zoom if zoom control is reset
                if ctrl.id == "zoom_absolute" and self._engine is not None:
                    self._engine.set_zoom(1.0)
                # Reset software sharpness if sharpness control is reset
                if ctrl.id == "sharpness" and self._engine is not None:
                    self._engine.set_sharpness(0.0)
                # Reset software pan if control is reset
                if ctrl.id == "pan_absolute" and self._engine is not None:
                    self._engine.set_pan(0.0)
                # Reset software tilt if control is reset
                if ctrl.id == "tilt_absolute" and self._engine is not None:
                    self._engine.set_tilt(0.0)
            self._resetting = False
