"""Effects page — toggle and configure OpenCV video effects."""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, GLib, GObject

from core.effects import EffectPipeline, EffectInfo, EffectCategory, EffectParam
from utils.i18n import _


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

    def _make_reset_button(self, cat: EffectCategory,
                           effs: list[EffectInfo]) -> Gtk.Button:
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

    def _add_effect_rows(self, group: Adw.PreferencesGroup,
                         effect: EffectInfo) -> None:
        # Toggle row for the effect
        toggle_row = Adw.SwitchRow(
            title=effect.name,
            subtitle=effect.description if hasattr(effect, "description") else "",
        )
        if effect.icon:
            toggle_row.set_icon_name(effect.icon)
        toggle_row.set_active(effect.enabled)
        toggle_row.update_property(
            [Gtk.AccessibleProperty.LABEL], [effect.name],
        )
        toggle_row.connect("notify::active", self._on_toggle, effect)
        group.add(toggle_row)

        # Parameter rows (always visible)
        if effect.params:
            for param in effect.params:
                param_row = self._make_param_row(effect, param)
                group.add(param_row)

    def _make_param_row(self, effect: EffectInfo, param: EffectParam) -> Adw.ActionRow:
        row = Adw.ActionRow(title=param.label)
        row.update_property(
            [Gtk.AccessibleProperty.LABEL], [param.label],
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

        return row

    def _on_toggle(self, row: Adw.SwitchRow, _pspec: Any,
                   effect: EffectInfo) -> None:
        enabled = row.get_active()
        effect.enabled = enabled
        self._pipeline.set_enabled(effect.effect_id, enabled)
        self.emit("effect-changed")

    def _on_param_changed(self, adj: Gtk.Adjustment, effect: EffectInfo,
                          param: EffectParam) -> None:
        key = f"{effect.effect_id}_{param.name}"
        if key in self._debounce_sources:
            GLib.source_remove(self._debounce_sources[key])
        self._debounce_sources[key] = GLib.timeout_add(
            50, self._apply_param, adj, effect, param, key,
        )

    def _apply_param(self, adj: Gtk.Adjustment, effect: EffectInfo,
                     param: EffectParam, key: str) -> bool:
        self._debounce_sources.pop(key, None)
        value = adj.get_value()
        param.value = value
        self._pipeline.set_param(effect.effect_id, param.name, value)
        self.emit("effect-changed")
        return False

    def _on_reset_category(self, _btn: Gtk.Button,
                           effs: list[EffectInfo]) -> None:
        for eff in effs:
            eff.enabled = False
            self._pipeline.set_enabled(eff.effect_id, False)
            self._pipeline.reset_effect(eff.effect_id)
            for param in eff.params:
                param.value = param.default
        self._rebuild()
        self.emit("effect-changed")

    def _rebuild(self) -> None:
        child = self._content.get_first_child()
        while child:
            next_c = child.get_next_sibling()
            self._content.remove(child)
            child = next_c
        self._debounce_sources.clear()
        self._build_ui()
