"""Inline notification revealer – replaces AdwToast for accessible feedback."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk, GLib

from utils.i18n import _

# CSS classes matching Adwaita semantic colours
_STYLE_MAP = {
    "info": "accent",
    "success": "success",
    "warning": "warning",
    "error": "error",
}


class InlineNotification(Gtk.Revealer):
    """Slide-down notification bar placed at the top of the preview area."""

    def __init__(self) -> None:
        super().__init__(
            transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
            reveal_child=False,
        )
        self._timeout_id: int | None = None

        self._box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
            margin_start=16,
            margin_end=16,
            margin_top=10,
            margin_bottom=10,
            halign=Gtk.Align.CENTER,
        )
        self._box.add_css_class("osd")
        self._box.add_css_class("notification-bar")

        self._icon = Gtk.Image(
            pixel_size=24,
        )
        self._label = Gtk.Label(
            hexpand=True,
            xalign=0,
            wrap=True,
            wrap_mode=2,  # WORD_CHAR
        )
        self._label.add_css_class("notification-label")

        dismiss = Gtk.Button.new_from_icon_name("window-close-symbolic")
        dismiss.add_css_class("flat")
        dismiss.add_css_class("circular")
        dismiss.set_tooltip_text(_("Dismiss"))
        dismiss.connect("clicked", lambda _b: self.dismiss())
        dismiss.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Dismiss notification")]
        )

        self._box.append(self._icon)
        self._box.append(self._label)
        self._box.append(dismiss)
        self.set_child(self._box)

    # -- public API ----------------------------------------------------------

    def notify_user(
        self, message: str, level: str = "info", timeout_ms: int = 3000
    ) -> None:
        """Show *message* with the given *level* (info/success/warning/error)."""
        # Remove prior CSS classes
        for css_cls in _STYLE_MAP.values():
            self._box.remove_css_class(css_cls)

        css_cls = _STYLE_MAP.get(level, "accent")
        self._box.add_css_class(css_cls)

        icon_map = {
            "info": "dialog-information-symbolic",
            "success": "emblem-ok-symbolic",
            "warning": "dialog-warning-symbolic",
            "error": "dialog-error-symbolic",
        }
        self._icon.set_from_icon_name(
            icon_map.get(level, "dialog-information-symbolic")
        )
        self._label.set_text(message)

        # Accessibility announcement
        self.update_property([Gtk.AccessibleProperty.LABEL], [message])

        self.set_reveal_child(True)

        if self._timeout_id is not None:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = None

        if timeout_ms > 0:
            self._timeout_id = GLib.timeout_add(timeout_ms, self._auto_dismiss)

    def dismiss(self) -> None:
        if self._timeout_id is not None:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = None
        self.set_reveal_child(False)

    # -- private -------------------------------------------------------------

    def _auto_dismiss(self) -> bool:
        self._timeout_id = None
        self.set_reveal_child(False)
        return False
