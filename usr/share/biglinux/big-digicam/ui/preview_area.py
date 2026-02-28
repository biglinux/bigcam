"""Preview area – GStreamer video preview with overlay toolbar and FPS counter."""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, Gdk, GLib, GObject

from core.stream_engine import StreamEngine
from ui.notification import InlineNotification
from utils.i18n import _


class PreviewArea(Gtk.Overlay):
    """Container with GStreamer video sink, FPS overlay and notification bar."""

    __gsignals__ = {
        "capture-requested": (GObject.SignalFlags.RUN_LAST, None, ()),
        "record-toggled": (GObject.SignalFlags.RUN_LAST, None, ()),
        "retry-requested": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self, stream_engine: StreamEngine) -> None:
        super().__init__()
        self._engine = stream_engine
        self._fps_timer: int | None = None
        self._show_fps: bool = True

        self.add_css_class("preview-area")

        # -- video picture ---------------------------------------------------
        self._picture = Gtk.Picture()
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._picture.set_hexpand(True)
        self._picture.set_vexpand(True)
        self._picture.add_css_class("preview-picture")

        # -- status page (no camera) -----------------------------------------
        self._status = Adw.StatusPage(
            icon_name="camera-web-symbolic",
            title=_("No camera"),
            description=_("Connect a camera or select one from the list above."),
        )
        self._status.set_hexpand(True)
        self._status.set_vexpand(True)

        # Retry button (hidden by default)
        self._retry_btn = Gtk.Button(label=_("Try again"))
        self._retry_btn.add_css_class("suggested-action")
        self._retry_btn.add_css_class("pill")
        self._retry_btn.set_halign(Gtk.Align.CENTER)
        self._retry_btn.set_visible(False)
        self._retry_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Try again")]
        )
        self._retry_btn.connect("clicked", lambda _b: self.emit("retry-requested"))
        self._status.set_child(self._retry_btn)

        self._retry_timer: int | None = None

        # -- stack (status / picture) ----------------------------------------
        self._stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
        )
        self._stack.add_named(self._status, "status")
        self._stack.add_named(self._picture, "preview")
        self._stack.set_visible_child_name("status")
        self.set_child(self._stack)

        # -- notification bar (top overlay) ----------------------------------
        self._notification = InlineNotification()
        self._notification.set_halign(Gtk.Align.FILL)
        self._notification.set_valign(Gtk.Align.START)
        self.add_overlay(self._notification)

        # -- FPS label (top-right overlay) -----------------------------------
        self._fps_label = Gtk.Label(label="")
        self._fps_label.add_css_class("osd")
        self._fps_label.add_css_class("fps-label")
        self._fps_label.set_halign(Gtk.Align.END)
        self._fps_label.set_valign(Gtk.Align.START)
        self._fps_label.set_margin_top(8)
        self._fps_label.set_margin_end(8)
        self._fps_label.set_visible(False)
        self.add_overlay(self._fps_label)

        # -- floating toolbar (bottom-center) --------------------------------
        self._toolbar = self._build_floating_toolbar()
        self._toolbar.set_halign(Gtk.Align.CENTER)
        self._toolbar.set_valign(Gtk.Align.END)
        self._toolbar.set_margin_bottom(16)
        self.add_overlay(self._toolbar)

        # -- engine signals --------------------------------------------------
        self._engine.connect("state-changed", self._on_state_changed)
        self._engine.connect("error", self._on_error)
        self._engine.connect("new-texture", self._on_new_texture)

    # -- floating toolbar ----------------------------------------------------

    def _build_floating_toolbar(self) -> Gtk.Box:
        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
            margin_start=16,
            margin_end=16,
            margin_top=8,
            margin_bottom=8,
        )
        box.add_css_class("osd")
        box.add_css_class("toolbar")

        # Capture button
        self._capture_btn = Gtk.Button.new_from_icon_name("camera-photo-symbolic")
        self._capture_btn.add_css_class("circular")
        self._capture_btn.add_css_class("suggested-action")
        self._capture_btn.set_tooltip_text(_("Capture photo"))
        self._capture_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Capture photo")]
        )
        self._capture_btn.connect("clicked", lambda _b: self.emit("capture-requested"))
        box.append(self._capture_btn)

        # Record button
        self._record_btn = Gtk.Button.new_from_icon_name("media-record-symbolic")
        self._record_btn.add_css_class("circular")
        self._record_btn.set_tooltip_text(_("Record video"))
        self._record_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Record video")]
        )
        self._record_btn.connect("clicked", lambda _b: self.emit("record-toggled"))
        box.append(self._record_btn)

        return box

    # -- signals / callbacks -------------------------------------------------

    def _on_state_changed(self, _engine: StreamEngine, state: str) -> None:
        if state == "playing":
            paintable = self._engine.paintable
            if paintable:
                self._picture.set_paintable(paintable)
            # For appsink mode, the picture gets updated via new-texture signal
            self._stack.set_visible_child_name("preview")
            self._fps_label.set_visible(self._show_fps)
            self._retry_btn.set_visible(False)
            self._cancel_retry_timer()
            self._start_fps_timer()
        else:
            self._stack.set_visible_child_name("status")
            self._fps_label.set_visible(False)
            self._stop_fps_timer()

    def _on_error(self, _engine: StreamEngine, message: str) -> None:
        self._notification.notify_user(message, "error", 5000)
        self._show_retry()

    def _on_new_texture(self, _engine: StreamEngine, texture: object) -> None:
        """Update the preview picture from appsink-rendered texture."""
        self._picture.set_paintable(texture)

    # -- FPS counter ---------------------------------------------------------

    def _start_fps_timer(self) -> None:
        self._stop_fps_timer()
        self._fps_timer = GLib.timeout_add(1000, self._update_fps)

    def _stop_fps_timer(self) -> None:
        tid = self._fps_timer
        self._fps_timer = None
        if tid is not None:
            GLib.source_remove(tid)

    def _update_fps(self) -> bool:
        if self._engine.is_playing():
            fps = self._engine.fps
            if fps > 0:
                self._fps_label.set_text(f"{fps:.0f} FPS")
            else:
                self._fps_label.set_text("⏵ Live")
            return True
        self._fps_label.set_visible(False)
        self._fps_timer = None
        return False

    def set_show_fps(self, show: bool) -> None:
        """Toggle FPS counter visibility."""
        self._show_fps = show
        if show and self._engine.is_playing():
            self._fps_label.set_visible(True)
        else:
            self._fps_label.set_visible(False)

    # -- public helpers ------------------------------------------------------

    @property
    def notification(self) -> InlineNotification:
        return self._notification

    def show_status(self, title: str, description: str = "",
                    icon: str = "camera-web-symbolic") -> None:
        self._status.set_title(title)
        self._status.set_description(description)
        self._status.set_icon_name(icon)
        self._retry_btn.set_visible(False)
        self._stack.set_visible_child_name("status")

    # -- retry helpers -------------------------------------------------------

    def start_retry_countdown(self, seconds: int = 10) -> None:
        """Start a countdown; show retry button after *seconds*."""
        self._cancel_retry_timer()
        self._retry_timer = GLib.timeout_add(seconds * 1000, self._show_retry)

    def _show_retry(self) -> bool:
        self._cancel_retry_timer()
        self._status.set_title(_("Connection failed"))
        self._status.set_description(
            _("Could not connect to the camera. Check the connection and try again.")
        )
        self._status.set_icon_name("dialog-warning-symbolic")
        self._retry_btn.set_visible(True)
        self._stack.set_visible_child_name("status")
        return False

    def _cancel_retry_timer(self) -> None:
        if self._retry_timer is not None:
            GLib.source_remove(self._retry_timer)
            self._retry_timer = None

    def set_recording_state(self, recording: bool) -> None:
        if recording:
            self._record_btn.set_icon_name("media-playback-stop-symbolic")
            self._record_btn.add_css_class("destructive-action")
            self._record_btn.set_tooltip_text(_("Stop recording"))
        else:
            self._record_btn.set_icon_name("media-record-symbolic")
            self._record_btn.remove_css_class("destructive-action")
            self._record_btn.set_tooltip_text(_("Record video"))
