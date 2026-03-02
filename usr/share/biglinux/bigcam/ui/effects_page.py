"""Effects page — toggle and configure OpenCV video effects."""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, GLib, GObject  # noqa: E402

from core.effects import EffectPipeline, EffectInfo, EffectCategory, EffectParam  # noqa: E402
from utils.i18n import _  # noqa: E402


_CATEGORY_LABELS = {
    EffectCategory.ADJUST: _("Adjustments"),
    EffectCategory.FILTER: _("Filters"),
    EffectCategory.ARTISTIC: _("Artistic"),
    EffectCategory.ADVANCED: _("Advanced"),
}

_CATEGORY_ICONS = {
    EffectCategory.ADJUST: "preferences-color-symbolic",
    EffectCategory.FILTER: "image-filter-symbolic",
    EffectCategory.ARTISTIC: "applications-graphics-symbolic",
    EffectCategory.ADVANCED: "emblem-system-symbolic",
}


class EffectsPage(Gtk.ScrolledWindow):
    """Sidebar page that lists all effects with toggles and parameter sliders."""

    __gsignals__ = {
        "effect-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self, effect_pipeline: EffectPipeline) -> None:
        super().__init__(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        self._pipeline = effect_pipeline
        self._debounce_sources: dict[str, int] = {}
        self._effect_widgets: dict[str, dict[str, Any]] = {}
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

        if not effect_pipeline.available:
            empty = Adw.StatusPage(
                icon_name="dialog-warning-symbolic",
                title=_("OpenCV not available"),
                description=_("Install python-opencv to enable video effects."),
            )
            self._content.append(empty)
            return

        self._build_ui()

    def _build_ui(self) -> None:
        effects = self._pipeline.get_effects()
        groups: dict[EffectCategory, list[EffectInfo]] = {}
        for eff in effects:
            groups.setdefault(eff.category, []).append(eff)

        order = [
            EffectCategory.ADJUST,
            EffectCategory.FILTER,
            EffectCategory.ARTISTIC,
            EffectCategory.ADVANCED,
        ]

        for cat in order:
            effs = groups.get(cat)
            if not effs:
                continue
            group = Adw.PreferencesGroup(
                title=_CATEGORY_LABELS.get(cat, cat.value),
            )
            group.set_header_suffix(self._make_reset_button(cat, effs))
            for eff in effs:
                self._add_effect_rows(group, eff)
            self._content.append(group)

    def _make_reset_button(
        self, cat: EffectCategory, effs: list[EffectInfo]
    ) -> Gtk.Button:
        btn = Gtk.Button.new_from_icon_name("edit-undo-symbolic")
        btn.add_css_class("flat")
        btn.set_tooltip_text(_("Reset to defaults"))
        btn.set_valign(Gtk.Align.CENTER)
        btn.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Reset %s effects") % _CATEGORY_LABELS.get(cat, "")],
        )
        btn.connect("clicked", self._on_reset_category, effs)
        return btn

    def _add_effect_rows(self, group: Adw.PreferencesGroup, effect: EffectInfo) -> None:
        if effect.params:
            # ExpanderRow with independent switch as suffix
            expander = Adw.ExpanderRow(
                title=effect.name,
                subtitle=effect.description if hasattr(effect, "description") else "",
            )
            if effect.icon:
                expander.set_icon_name(effect.icon)
            expander.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [effect.name],
            )
            # Add switch as suffix (independent of expansion)
            switch = Gtk.Switch()
            switch.set_active(effect.enabled)
            switch.set_valign(Gtk.Align.CENTER)
            switch.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [effect.name],
            )
            switch.connect("notify::active", self._on_switch_toggle, effect)
            expander.add_suffix(switch)
            self._effect_widgets[effect.effect_id] = {"switch": switch, "params": {}}
            # Replace internal arrow icon
            self._replace_arrow_icon(expander, "pan-up-symbolic")
            for param in effect.params:
                param_row = self._make_param_row(effect, param)
                expander.add_row(param_row)
            group.add(expander)
        else:
            # Simple toggle for effects without parameters
            toggle_row = Adw.SwitchRow(
                title=effect.name,
                subtitle=effect.description if hasattr(effect, "description") else "",
            )
            if effect.icon:
                toggle_row.set_icon_name(effect.icon)
            toggle_row.set_active(effect.enabled)
            toggle_row.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [effect.name],
            )
            toggle_row.connect("notify::active", self._on_toggle, effect)
            self._effect_widgets[effect.effect_id] = {
                "toggle": toggle_row,
                "params": {},
            }
            group.add(toggle_row)

    def _make_param_row(self, effect: EffectInfo, param: EffectParam) -> Adw.ActionRow:
        row = Adw.ActionRow(title=param.label)
        row.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [param.label],
        )

        adj = Gtk.Adjustment(
            value=param.value,
            lower=param.min_val,
            upper=param.max_val,
            step_increment=param.step,
        )
        scale = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=adj,
            hexpand=True,
            draw_value=True,
            value_pos=Gtk.PositionType.LEFT,
        )
        scale.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [param.label],
        )
        scale.set_size_request(180, -1)

        # Set digits based on step
        if param.step >= 1:
            scale.set_digits(0)
        elif param.step >= 0.1:
            scale.set_digits(1)
        else:
            scale.set_digits(2)

        adj.connect("value-changed", self._on_param_changed, effect, param)
        row.add_suffix(scale)

        widgets = self._effect_widgets.get(effect.effect_id)
        if widgets is not None:
            widgets["params"][param.name] = adj

        return row

    def _on_toggle(self, row: Adw.SwitchRow, _pspec: Any, effect: EffectInfo) -> None:
        if self._resetting:
            return
        enabled = row.get_active()
        effect.enabled = enabled
        self._pipeline.set_enabled(effect.effect_id, enabled)
        self.emit("effect-changed")

    def _on_switch_toggle(
        self, switch: Gtk.Switch, _pspec: Any, effect: EffectInfo
    ) -> None:
        if self._resetting:
            return
        enabled = switch.get_active()
        effect.enabled = enabled
        self._pipeline.set_enabled(effect.effect_id, enabled)
        self.emit("effect-changed")

    @staticmethod
    def _replace_arrow_icon(widget: Gtk.Widget, icon_name: str) -> None:
        """Walk the ExpanderRow widget tree and replace the arrow icon."""
        child = widget.get_first_child()
        while child:
            if isinstance(child, Gtk.Image):
                if "expander-row-arrow" in (child.get_css_classes() or []):
                    child.set_from_icon_name(icon_name)
                    return
            EffectsPage._replace_arrow_icon(child, icon_name)
            child = child.get_next_sibling()

    def _on_param_changed(
        self, adj: Gtk.Adjustment, effect: EffectInfo, param: EffectParam
    ) -> None:
        key = f"{effect.effect_id}_{param.name}"
        if key in self._debounce_sources:
            GLib.source_remove(self._debounce_sources[key])
        self._debounce_sources[key] = GLib.timeout_add(
            50,
            self._apply_param,
            adj,
            effect,
            param,
            key,
        )

    def _apply_param(
        self, adj: Gtk.Adjustment, effect: EffectInfo, param: EffectParam, key: str
    ) -> bool:
        self._debounce_sources.pop(key, None)
        value = adj.get_value()
        param.value = value
        self._pipeline.set_param(effect.effect_id, param.name, value)
        self.emit("effect-changed")
        return False

    def _on_reset_category(self, _btn: Gtk.Button, effs: list[EffectInfo]) -> None:
        self._resetting = True
        for eff in effs:
            eff.enabled = False
            self._pipeline.set_enabled(eff.effect_id, False)
            self._pipeline.reset_effect(eff.effect_id)
            for param in eff.params:
                param.value = param.default
            widgets = self._effect_widgets.get(eff.effect_id)
            if widgets:
                toggle = widgets.get("toggle")
                switch = widgets.get("switch")
                if toggle:
                    toggle.set_active(False)
                if switch:
                    switch.set_active(False)
                for pname, adj in widgets.get("params", {}).items():
                    for param in eff.params:
                        if param.name == pname:
                            adj.set_value(param.default)
                            break
        self._resetting = False
        self.emit("effect-changed")

    def _rebuild(self) -> None:
        child = self._content.get_first_child()
        while child:
            next_c = child.get_next_sibling()
            self._content.remove(child)
            child = next_c
        self._debounce_sources.clear()
        self._effect_widgets.clear()
        self._build_ui()
