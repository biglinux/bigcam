"""Preview area – GStreamer video preview with overlay toolbar and FPS counter."""

from __future__ import annotations


import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, Graphene, GLib, GObject

from core.audio_monitor import AudioMonitor
from core.stream_engine import StreamEngine
from utils.i18n import _


class MirroredPicture(Gtk.Picture):
    """Gtk.Picture that can be horizontally mirrored via GskTransform."""

    def __init__(self) -> None:
        super().__init__()
        self._mirror = False

    @property
    def mirror(self) -> bool:
        return self._mirror

    @mirror.setter
    def mirror(self, value: bool) -> None:
        if self._mirror != value:
            self._mirror = value
            self.queue_draw()

    def do_snapshot(self, snapshot: Gtk.Snapshot) -> None:
        if self._mirror:
            w = self.get_width()
            # Translate to right edge, scale X by -1 to mirror
            point = Graphene.Point()
            point.x = w
            point.y = 0
            snapshot.save()
            snapshot.translate(point)
            snapshot.scale(-1, 1)
            Gtk.Picture.do_snapshot(self, snapshot)
            snapshot.restore()
        else:
            Gtk.Picture.do_snapshot(self, snapshot)


class PreviewArea(Gtk.Overlay):
    """Container with GStreamer video sink, FPS overlay and toast notifications."""

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
        self._last_error: str = ""
        self._progress_pulse_id: int | None = None

        self.add_css_class("preview-area")

        # -- video picture ---------------------------------------------------
        self._picture = MirroredPicture()
        self._picture.set_content_fit(Gtk.ContentFit.COVER)
        self._picture.set_hexpand(True)
        self._picture.set_vexpand(True)
        self._picture.set_halign(Gtk.Align.FILL)
        self._picture.set_valign(Gtk.Align.FILL)
        self._picture.add_css_class("preview-picture")

        # -- status page (no camera) -----------------------------------------
        self._status = Adw.StatusPage(
            icon_name="camera-web-symbolic",
            title=_("No camera"),
            description=_("Connect a camera or select one from the list above."),
        )
        self._status.add_css_class("status-overlay")
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

        # -- toast overlay wraps the stack (quick feedback at bottom) --------
        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(self._stack)

        # -- banner at the top (persistent process messages) -----------------
        self._banner = Adw.Banner()
        self._banner.set_revealed(False)
        self._banner_timeout: int | None = None

        # -- progress bar (top, thin, loading indicator) ---------------------
        self._top_progress = Gtk.ProgressBar()
        self._top_progress.add_css_class("osd")
        self._top_progress.set_halign(Gtk.Align.FILL)
        self._top_progress.set_hexpand(True)
        self._top_progress.set_visible(False)

        # Pack progress + banner + toast_overlay vertically
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content_box.append(self._top_progress)
        content_box.append(self._banner)
        content_box.append(self._toast_overlay)
        self._toast_overlay.set_vexpand(True)
        self.set_child(content_box)

        # -- FPS label (right side, centered vertically) -------------------
        self._fps_label = Gtk.Label(label="")
        self._fps_label.add_css_class("fps-label")
        self._fps_label.set_halign(Gtk.Align.END)
        self._fps_label.set_valign(Gtk.Align.CENTER)
        self._fps_label.set_margin_end(12)
        self._fps_label.set_visible(False)
        self.add_overlay(self._fps_label)

        # -- floating toolbar (bottom-center) --------------------------------
        self._toolbar = self._build_floating_toolbar()
        self._toolbar.set_halign(Gtk.Align.CENTER)
        self._toolbar.set_valign(Gtk.Align.END)
        self._toolbar.set_margin_bottom(16)
        self.add_overlay(self._toolbar)

        # -- countdown label (big centered number) --------------------------
        self._countdown_label = Gtk.Label(label="")
        self._countdown_label.add_css_class("countdown-overlay")
        self._countdown_label.set_halign(Gtk.Align.CENTER)
        self._countdown_label.set_valign(Gtk.Align.CENTER)
        self._countdown_label.set_visible(False)
        self.add_overlay(self._countdown_label)
        self._countdown_timer_id: int | None = None

        # -- grid overlay (rule-of-thirds) ----------------------------------
        self._grid_drawing = Gtk.DrawingArea()
        self._grid_drawing.set_draw_func(self._draw_grid)
        self._grid_drawing.set_halign(Gtk.Align.FILL)
        self._grid_drawing.set_valign(Gtk.Align.FILL)
        self._grid_drawing.set_hexpand(True)
        self._grid_drawing.set_vexpand(True)
        self._grid_drawing.set_can_target(False)
        self._grid_drawing.set_visible(False)
        self.add_overlay(self._grid_drawing)

        # -- audio volume overlay (left side, centered vertically) ----------
        self._audio_monitor: AudioMonitor | None = None
        self._audio_box = self._build_audio_overlay()
        self._audio_box.set_halign(Gtk.Align.START)
        self._audio_box.set_valign(Gtk.Align.CENTER)
        self._audio_box.set_margin_start(12)
        self._audio_box.set_visible(False)
        self.add_overlay(self._audio_box)

        # -- engine signals --------------------------------------------------
        self._engine.connect("state-changed", self._on_state_changed)
        self._engine.connect("error", self._on_error)
        self._engine.connect("new-texture", self._on_new_texture)

    def set_mirror(self, mirror: bool) -> None:
        """Toggle horizontal mirror on the preview picture."""
        self._picture.mirror = mirror

    # -- immersion helpers ---------------------------------------------------

    def immersion_widgets(self) -> list[Gtk.Widget]:
        """Return overlay widgets that should fade during immersion."""
        return [
            self._toolbar,
            self._fps_label,
            self._audio_box,
            self._grid_drawing,
            self._top_progress,
            self._banner,
        ]

    def is_countdown_active(self) -> bool:
        """True while a capture countdown timer is running."""
        return self._countdown_timer_id is not None

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

        self._capture_btn = Gtk.Button.new_from_icon_name("camera-photo-symbolic")
        self._capture_btn.add_css_class("circular")
        self._capture_btn.add_css_class("suggested-action")
        self._capture_btn.add_css_class("capture-button")
        self._capture_btn.set_tooltip_text(_("Capture photo"))
        self._capture_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Capture photo")]
        )
        self._capture_btn.connect("clicked", lambda _b: self.emit("capture-requested"))
        box.append(self._capture_btn)

        # Record button — classic REC style (red dot with circular border)
        self._rec_dot = Gtk.Box()
        self._rec_dot.add_css_class("rec-dot")
        self._rec_stop = Gtk.Box()
        self._rec_stop.add_css_class("rec-stop-icon")
        self._rec_stop.set_visible(False)
        self._rec_stop.set_halign(Gtk.Align.CENTER)
        self._rec_stop.set_valign(Gtk.Align.CENTER)
        self._rec_overlay = Gtk.Overlay()
        self._rec_overlay.set_child(self._rec_dot)
        self._rec_overlay.add_overlay(self._rec_stop)
        self._rec_overlay.set_halign(Gtk.Align.CENTER)
        self._rec_overlay.set_valign(Gtk.Align.CENTER)
        self._record_btn = Gtk.Button()
        self._record_btn.set_child(self._rec_overlay)
        self._record_btn.add_css_class("record-button")
        self._is_recording = False
        self._record_btn.set_tooltip_text(_("Record video (Ctrl+R)"))
        self._record_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Record video")]
        )
        self._record_btn.connect("clicked", lambda _b: self.emit("record-toggled"))

        # Hover controller for stop icon during recording
        hover_ctrl = Gtk.EventControllerMotion()
        hover_ctrl.connect("enter", self._on_rec_hover_enter)
        hover_ctrl.connect("leave", self._on_rec_hover_leave)
        self._record_btn.add_controller(hover_ctrl)

        box.append(self._record_btn)

        return box

    # -- audio overlay -------------------------------------------------------

    def _build_audio_overlay(self) -> Gtk.Box:
        """Build audio overlay with checkboxes + mute icon on top row,
        and per-source vertical volume sliders that appear on hover."""
        # Main container: vertical — top row of checks + slider popups below
        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=0,
            margin_start=6,
            margin_end=6,
            margin_top=4,
            margin_bottom=4,
        )
        outer.add_css_class("audio-overlay")

        # Top row: checkboxes + mute icon
        top_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=2
        )
        top_row.set_valign(Gtk.Align.CENTER)

        self._audio_checks_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=0
        )
        top_row.append(self._audio_checks_box)

        self._mute_btn = Gtk.Button.new_from_icon_name(
            "audio-volume-medium-symbolic"
        )
        self._mute_btn.add_css_class("circular")
        self._mute_btn.add_css_class("audio-overlay-btn")
        self._mute_btn.set_tooltip_text(_("Mute"))
        self._mute_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Mute")]
        )
        self._mute_btn.connect("clicked", self._on_mute_clicked)
        top_row.append(self._mute_btn)

        outer.append(top_row)

        # Container for vertical volume sliders (shown on hover)
        self._vol_sliders_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=0
        )
        self._vol_sliders_box.set_visible(False)
        self._vol_sliders_box.set_halign(Gtk.Align.START)
        outer.append(self._vol_sliders_box)

        # Track per-source slider widgets: {source_name: Gtk.Scale}
        self._source_scales: dict[str, Gtk.Scale] = {}
        # Track per-source scale signal handlers
        self._source_scale_handlers: dict[str, int] = {}

        return outer

    def set_audio_monitor(self, monitor: AudioMonitor | None) -> None:
        """Bind an AudioMonitor to the overlay controls."""
        if self._audio_monitor:
            try:
                self._audio_monitor.disconnect_by_func(self._on_sources_changed)
                self._audio_monitor.disconnect_by_func(self._on_mute_changed)
            except TypeError:
                pass
        self._audio_monitor = monitor
        if monitor:
            monitor.connect("sources-changed", self._on_sources_changed)
            monitor.connect("mute-changed", self._on_mute_changed)
            self._update_mute_icon(monitor.muted)
        else:
            self._audio_box.set_visible(False)

    def _on_sources_changed(self, mon: AudioMonitor) -> None:
        # Clear old checkboxes
        while True:
            child = self._audio_checks_box.get_first_child()
            if child is None:
                break
            self._audio_checks_box.remove(child)

        # Clear old volume sliders
        while True:
            child = self._vol_sliders_box.get_first_child()
            if child is None:
                break
            self._vol_sliders_box.remove(child)
        self._source_scales.clear()
        self._source_scale_handlers.clear()
        self._vol_sliders_box.set_visible(False)

        sources = mon.sources
        if not sources:
            self._audio_box.set_visible(False)
            return

        for idx, (src_name, label) in enumerate(sources, start=1):
            # -- Checkbox item container (vertical: checkbox on top, slider below) --
            item_box = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=0
            )
            item_box.add_css_class("audio-source-item")

            check = Gtk.CheckButton(label=str(idx))
            check.set_active(mon.is_active(src_name))
            check.set_tooltip_text(label)
            check.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [f"{label} – audio {idx}"],
            )
            check.connect("toggled", self._on_audio_check_toggled, src_name)
            item_box.append(check)

            self._audio_checks_box.append(item_box)

            # -- Vertical volume slider (in sliders box, aligned under checkbox) --
            slider_container = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=2
            )
            slider_container.add_css_class("audio-vol-slider-container")
            slider_container.set_halign(Gtk.Align.CENTER)

            # "+" label at top (max volume)
            plus_label = Gtk.Label(label="+")
            plus_label.add_css_class("audio-vol-label")
            plus_label.set_halign(Gtk.Align.CENTER)
            slider_container.append(plus_label)

            vol_scale = Gtk.Scale.new_with_range(
                Gtk.Orientation.VERTICAL, 0.0, 1.0, 0.05
            )
            vol_scale.set_inverted(True)  # top = max, bottom = min
            vol_scale.set_value(mon.get_source_volume(src_name))
            vol_scale.set_draw_value(False)
            vol_scale.set_size_request(-1, 100)
            vol_scale.add_css_class("audio-vol-vertical")
            vol_scale.set_halign(Gtk.Align.CENTER)
            vol_scale.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [f"{label} – volume"],
            )
            handler_id = vol_scale.connect(
                "value-changed", self._on_source_vol_changed, src_name
            )
            self._source_scales[src_name] = vol_scale
            self._source_scale_handlers[src_name] = handler_id
            slider_container.append(vol_scale)

            # "−" label at bottom (min volume)
            minus_label = Gtk.Label(label="−")
            minus_label.add_css_class("audio-vol-label")
            minus_label.set_halign(Gtk.Align.CENTER)
            slider_container.append(minus_label)

            self._vol_sliders_box.append(slider_container)

        # Add hover controller to the entire audio overlay to show/hide sliders
        if not hasattr(self, "_audio_hover_ctrl"):
            self._audio_hover_ctrl = Gtk.EventControllerMotion()
            self._audio_hover_ctrl.connect("enter", self._on_audio_hover_enter)
            self._audio_hover_ctrl.connect("leave", self._on_audio_hover_leave)
            self._audio_box.add_controller(self._audio_hover_ctrl)

        self._audio_box.set_visible(True)

    def _on_audio_hover_enter(self, *_args) -> None:
        """Show volume sliders when mouse enters the audio overlay."""
        if self._source_scales:
            self._vol_sliders_box.set_visible(True)

    def _on_audio_hover_leave(self, *_args) -> None:
        """Hide volume sliders when mouse leaves the audio overlay."""
        self._vol_sliders_box.set_visible(False)

    def _on_audio_check_toggled(
        self, check: Gtk.CheckButton, source_name: str
    ) -> None:
        if not self._audio_monitor:
            return
        is_active = self._audio_monitor.is_active(source_name)
        want_active = check.get_active()
        if want_active != is_active:
            self._audio_monitor.toggle_source(source_name)

    def _on_mute_changed(self, _mon: AudioMonitor, muted: bool) -> None:
        self._update_mute_icon(muted)

    def _update_mute_icon(self, muted: bool) -> None:
        if muted:
            icon = "audio-volume-muted-symbolic"
            self._mute_btn.set_tooltip_text(_("Unmute"))
        else:
            icon = "audio-volume-high-symbolic"
            self._mute_btn.set_tooltip_text(_("Mute"))
        self._mute_btn.set_icon_name(icon)

    def _on_mute_clicked(self, _btn: Gtk.Button) -> None:
        if self._audio_monitor:
            self._audio_monitor.toggle_mute()

    def _on_source_vol_changed(
        self, scale: Gtk.Scale, source_name: str
    ) -> None:
        """Handle per-source vertical volume slider change."""
        if self._audio_monitor:
            self._audio_monitor.set_source_volume(source_name, scale.get_value())

    # -- signals / callbacks -------------------------------------------------

    def _on_state_changed(self, _engine: StreamEngine, state: str) -> None:
        if state == "playing":
            self._stop_progress_pulse()
            self._last_error = ""
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
        self._last_error = message
        self.notify_user(message, "error", 5000)
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

    def set_toolbar_visible(self, visible: bool) -> None:
        """Show or hide the built-in floating toolbar."""
        self._toolbar.set_visible(visible)

    # -- public helpers ------------------------------------------------------

    @property
    def notification(self) -> "PreviewArea":
        """Backward-compatible accessor – returns self so
        ``preview.notification.notify_user(...)`` keeps working."""
        return self

    def notify_user(
        self,
        message: str,
        level: str = "info",
        timeout_ms: int = 3000,
        progress: bool = False,
    ) -> None:
        """Show a banner at the top of the preview area.

        If *timeout_ms* is 0, the banner stays until ``dismiss()`` is called.
        If *progress* is True, show the pulsing progress bar at top.
        """
        if self._banner_timeout is not None:
            GLib.source_remove(self._banner_timeout)
            self._banner_timeout = None

        self._banner.set_title(message)
        self._banner.set_revealed(True)

        if progress:
            self._stop_progress_pulse()
            self._top_progress.set_visible(True)
            self._progress_pulse_id = GLib.timeout_add(80, self._pulse_progress)
        else:
            self._stop_progress_pulse()

        if timeout_ms > 0:
            self._banner_timeout = GLib.timeout_add(
                timeout_ms, self._auto_dismiss_banner
            )

    def _show_loading(self, message: str) -> None:
        self._status.set_icon_name("camera-web-symbolic")
        self._status.set_title(_("Please wait…"))
        self._status.set_description(message)
        self._status.set_child(None)
        self._retry_btn.set_visible(False)
        self._stack.set_visible_child_name("status")

        # Show pulsing progress bar at the top
        self._stop_progress_pulse()
        self._top_progress.set_visible(True)
        self._progress_pulse_id = GLib.timeout_add(80, self._pulse_progress)

    def _pulse_progress(self) -> bool:
        if self._top_progress.get_visible():
            self._top_progress.pulse()
            return True
        self._progress_pulse_id = None
        return False

    def _stop_progress_pulse(self) -> None:
        tid = self._progress_pulse_id
        self._progress_pulse_id = None
        if tid is not None:
            GLib.source_remove(tid)
        self._top_progress.set_visible(False)

    def dismiss(self) -> None:
        """Hide the banner and restore status page."""
        if self._banner_timeout is not None:
            GLib.source_remove(self._banner_timeout)
            self._banner_timeout = None
        self._banner.set_revealed(False)
        self._stop_progress_pulse()
        # Restore status page defaults after loading
        self._status.set_icon_name("camera-web-symbolic")
        self._status.set_child(self._retry_btn)

    def _auto_dismiss_banner(self) -> bool:
        self._banner_timeout = None
        self._banner.set_revealed(False)
        return GLib.SOURCE_REMOVE

    def show_status(
        self,
        title: str,
        description: str = "",
        icon: str = "camera-web-symbolic",
        loading: bool = False,
    ) -> None:
        if loading:
            self._show_loading(description)
            return
        self._stop_progress_pulse()
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
        error = self._last_error
        if error and _("Camera in use by:") in error:
            self._status.set_title(_("Camera busy"))
            self._status.set_description(error)
        elif error and _("Camera is being used") in error:
            self._status.set_title(_("Camera busy"))
            self._status.set_description(error)
        else:
            self._status.set_title(_("Connection failed"))
            self._status.set_description(
                _(
                    "Could not connect to the camera. Check the connection and try again."
                )
            )
        self._status.set_icon_name("dialog-warning-symbolic")
        self._retry_btn.set_visible(True)
        self._stack.set_visible_child_name("status")
        self._last_error = ""
        return False

    def _cancel_retry_timer(self) -> None:
        if self._retry_timer is not None:
            GLib.source_remove(self._retry_timer)
            self._retry_timer = None

    def set_recording_state(self, recording: bool) -> None:
        self._is_recording = recording
        if recording:
            self._record_btn.add_css_class("recording")
            self._rec_dot.add_css_class("recording")
            self._record_btn.set_tooltip_text(_("Stop recording"))
        else:
            self._record_btn.remove_css_class("recording")
            self._rec_dot.remove_css_class("recording")
            self._rec_stop.set_visible(False)
            self._rec_dot.set_visible(True)
            self._record_btn.set_tooltip_text(_("Record video (Ctrl+R)"))

    def _on_rec_hover_enter(self, *_args) -> None:
        if self._is_recording:
            self._rec_dot.set_visible(False)
            self._rec_stop.set_visible(True)

    def _on_rec_hover_leave(self, *_args) -> None:
        if self._is_recording:
            self._rec_stop.set_visible(False)
            self._rec_dot.set_visible(True)

    # -- grid overlay --------------------------------------------------------

    def set_grid_visible(self, visible: bool) -> None:
        self._grid_drawing.set_visible(visible)

    def _draw_grid(self, area: Gtk.DrawingArea, cr, width: int, height: int) -> None:
        """Draw rule-of-thirds grid lines."""
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.4)
        cr.set_line_width(1.0)
        # Vertical lines at 1/3 and 2/3
        for i in (1, 2):
            x = width * i / 3
            cr.move_to(x, 0)
            cr.line_to(x, height)
        # Horizontal lines at 1/3 and 2/3
        for i in (1, 2):
            y = height * i / 3
            cr.move_to(0, y)
            cr.line_to(width, y)
        cr.stroke()

    # -- countdown timer -----------------------------------------------------

    def start_countdown(self, seconds: int, callback) -> None:
        """Show a countdown overlay, then call *callback* when it reaches 0."""
        self._cancel_countdown()
        self._countdown_remaining = seconds
        self._countdown_callback = callback
        self._countdown_label.set_label(str(seconds))
        self._countdown_label.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("{n} seconds remaining").format(n=seconds)],
        )
        self._countdown_label.set_visible(True)
        self._countdown_timer_id = GLib.timeout_add(1000, self._tick_countdown)

    def _tick_countdown(self) -> bool:
        self._countdown_remaining -= 1
        if self._countdown_remaining > 0:
            self._countdown_label.set_label(str(self._countdown_remaining))
            self._countdown_label.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [_("{n} seconds remaining").format(n=self._countdown_remaining)],
            )
            return True
        self._countdown_label.set_visible(False)
        self._countdown_timer_id = None
        if self._countdown_callback:
            self._countdown_callback()
            self._countdown_callback = None
        return False

    def _cancel_countdown(self) -> None:
        if self._countdown_timer_id is not None:
            GLib.source_remove(self._countdown_timer_id)
            self._countdown_timer_id = None
        self._countdown_label.set_visible(False)
        self._countdown_callback = None
