"""Immersion controller – auto-hide all UI chrome on pointer/keyboard inactivity.

When the user stops interacting with the window, all header bars, sidebars,
toolbars and overlays fade out smoothly, leaving only the camera feed visible.
Any pointer movement or key press instantly restores the full UI.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gdk, GLib, Gtk

if TYPE_CHECKING:
    from gi.repository import Adw

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------
_INACTIVITY_MS = 2000       # Time before UI hides (2 s)
_FADE_DURATION_MS = 400     # Smooth fade-out duration
_FADE_STEPS = 10            # Discrete opacity steps during fade


class ImmersionController:
    """Centralised manager for the immersive auto-hide behaviour.

    * Monitors pointer motion and key-press events on a top-level window.
    * After *_INACTIVITY_MS* of silence, smoothly fades registered widgets to
      ``opacity 0`` and disables their hit-testing (``can_target = False``).
    * Any subsequent activity *instantly* restores full opacity and
      interactivity — no animation delay on re-entry.
    * Optionally manages a :class:`Gtk.Revealer` for the header bar so that
      it collapses vertically, giving the preview more space.
    * Hides the mouse cursor while immersed.
    """

    def __init__(self, window: Gtk.Window) -> None:
        self._window = window
        self._timer_id: int | None = None
        self._fade_timer_id: int | None = None
        self._fade_step: int = 0
        self._is_immersed: bool = False
        self._inhibit_count: int = 0

        # Managed widgets
        self._header_revealer: Gtk.Revealer | None = None
        self._extra_revealers: list[Gtk.Revealer] = []
        self._fade_widgets: list[Gtk.Widget] = []
        self._split_view: Adw.OverlaySplitView | None = None
        self._root_box: Gtk.Widget | None = None

        # Blank cursor
        self._blank_cursor = Gdk.Cursor.new_from_name("none")

        # Track whether the pointer is inside the window
        self._pointer_inside: bool = False

        # --- Event controllers on the window level -------------------------
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("enter", self._on_pointer_enter)
        motion.connect("leave", self._on_pointer_leave)
        window.add_controller(motion)

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key_activity)
        window.add_controller(key)

    # -- Registration -------------------------------------------------------

    def set_header_revealer(self, revealer: Gtk.Revealer) -> None:
        """Register the revealer that wraps the header bar."""
        self._header_revealer = revealer

    def set_split_view(self, split_view: "Adw.OverlaySplitView") -> None:
        """Register the split view so its sidebar can be hidden on immersion."""
        self._split_view = split_view

    def set_root_box(self, box: Gtk.Widget) -> None:
        """Register the root container for background darkening."""
        self._root_box = box

    def add_fade_widget(self, widget: Gtk.Widget) -> None:
        """Register a widget whose opacity will be animated on hide/show."""
        if widget not in self._fade_widgets:
            self._fade_widgets.append(widget)

    def add_revealer(self, revealer: Gtk.Revealer) -> None:
        """Register an extra revealer to hide/show alongside the header."""
        if revealer not in self._extra_revealers:
            self._extra_revealers.append(revealer)

    # -- Inhibition ---------------------------------------------------------

    def inhibit(self) -> None:
        """Increment the inhibit counter (e.g. popover open, countdown active).

        While inhibited the controller will *not* enter immersion.  If
        already immersed, the UI is instantly restored.
        """
        self._inhibit_count += 1
        if self._is_immersed:
            self._show_ui()

    def uninhibit(self) -> None:
        """Decrement the inhibit counter and restart the inactivity timer."""
        self._inhibit_count = max(0, self._inhibit_count - 1)
        if self._inhibit_count == 0:
            self._restart_timer()

    def present_dialog(self, dialog, parent=None) -> None:
        """Present an Adw.Dialog while inhibiting immersion.

        Automatically uninhibits when the dialog is closed.
        """
        self.inhibit()
        dialog.connect("closed", lambda *_: self.uninhibit())
        if parent is not None:
            dialog.present(parent)
        else:
            dialog.present()

    @property
    def is_immersed(self) -> bool:
        return self._is_immersed

    # -- Event handlers -----------------------------------------------------

    def _on_pointer_enter(self, *_args: object) -> None:
        """Mouse entered the window — cancel hide timer and show UI."""
        self._pointer_inside = True
        if self._is_immersed:
            self._show_ui()
        self._cancel_timer()

    def _on_pointer_leave(self, *_args: object) -> None:
        """Mouse left the window — start inactivity timer to hide UI."""
        self._pointer_inside = False
        self._restart_timer()

    def _on_motion(self, *_args: object) -> None:
        """Mouse moved inside the window — restore UI if hidden."""
        if self._is_immersed:
            self._show_ui()
        # While pointer is inside, no need to restart hide timer
        # (UI stays visible as long as pointer is in the window)

    def _on_key_activity(
        self,
        _ctrl: Gtk.EventControllerKey,
        keyval: int,
        keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if self._is_immersed:
            self._show_ui()
        self._restart_timer()
        return False  # propagate

    # -- Timer management ---------------------------------------------------

    def _cancel_timer(self) -> None:
        if self._timer_id is not None:
            GLib.source_remove(self._timer_id)
            self._timer_id = None

    def _restart_timer(self) -> None:
        self._cancel_timer()
        if self._inhibit_count == 0 and not self._pointer_inside:
            self._timer_id = GLib.timeout_add(
                _INACTIVITY_MS, self._on_inactivity_timeout
            )

    def _is_any_popover_mapped(self, widget: Gtk.Widget) -> bool:
        if isinstance(widget, Gtk.Popover) and widget.get_mapped():
            return True
        child = widget.get_first_child()
        while child is not None:
            if self._is_any_popover_mapped(child):
                return True
            child = child.get_next_sibling()
        return False

    def _on_inactivity_timeout(self) -> bool:
        if self._inhibit_count == 0:
            if self._is_any_popover_mapped(self._window):
                return True  # Keep timeout alive while popover is open
            self._timer_id = None
            self._begin_fade_out()
            return False

        self._timer_id = None
        return False

    # -- Fade out (smooth) --------------------------------------------------

    def _begin_fade_out(self) -> None:
        if self._is_immersed:
            return
        self._is_immersed = True
        self._fade_step = 0

        # Header revealer: smooth slide-up
        if self._header_revealer:
            self._header_revealer.set_transition_duration(_FADE_DURATION_MS)
            self._header_revealer.set_reveal_child(False)

        # Extra revealers: crossfade out
        for rev in self._extra_revealers:
            rev.set_transition_duration(_FADE_DURATION_MS)
            rev.set_reveal_child(False)

        # Split view: hide sidebar on immersion
        if self._split_view and self._split_view.get_show_sidebar():
            self._split_view.set_show_sidebar(False)

        # Root box: dark bg
        if self._root_box:
            self._root_box.add_css_class("immersion-active")

        # Start opacity animation on registered widgets
        interval = max(1, _FADE_DURATION_MS // _FADE_STEPS)
        self._fade_timer_id = GLib.timeout_add(interval, self._fade_tick)

        # Hide the mouse cursor
        self._window.set_cursor(self._blank_cursor)
        log.debug("Immersion: fade-out started")

    def _fade_tick(self) -> bool:
        self._fade_step += 1
        t = self._fade_step / _FADE_STEPS
        # Ease-out quad: opacity = (1 - t)^2  (decelerating curve)
        opacity = (1.0 - t) ** 2

        for w in self._fade_widgets:
            w.set_opacity(opacity)

        if self._fade_step >= _FADE_STEPS:
            self._fade_timer_id = None
            self._complete_fade_out()
            return False
        return True

    def _complete_fade_out(self) -> None:
        for w in self._fade_widgets:
            w.set_opacity(0.0)
            w.set_can_target(False)
        log.debug("Immersion: fully hidden")

    # -- Show UI (instant) --------------------------------------------------

    def _show_ui(self) -> None:
        if not self._is_immersed:
            return
        self._is_immersed = False

        # Cancel any ongoing fade animation
        if self._fade_timer_id is not None:
            GLib.source_remove(self._fade_timer_id)
            self._fade_timer_id = None

        # Header revealer: instant restore
        if self._header_revealer:
            self._header_revealer.set_transition_duration(0)
            self._header_revealer.set_reveal_child(True)

        # Extra revealers: instant restore
        for rev in self._extra_revealers:
            rev.set_transition_duration(0)
            rev.set_reveal_child(True)

        # Root box: remove dark styling
        if self._root_box:
            self._root_box.remove_css_class("immersion-active")

        # Widgets: instant full opacity + re-enable interaction
        for w in self._fade_widgets:
            w.set_opacity(1.0)
            w.set_can_target(True)

        # Restore mouse cursor
        self._window.set_cursor(None)

        # Restart inactivity timer
        self._restart_timer()
        log.debug("Immersion: UI restored")

    # -- Cleanup ------------------------------------------------------------

    def cleanup(self) -> None:
        """Cancel all pending timers.  Call on window close."""
        for attr in ("_timer_id", "_fade_timer_id"):
            tid = getattr(self, attr, None)
            if tid is not None:
                GLib.source_remove(tid)
                setattr(self, attr, None)
