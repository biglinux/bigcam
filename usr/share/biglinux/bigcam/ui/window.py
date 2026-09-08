"""Main application window – OverlaySplitView layout with preview + overlay sidebar."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gtk, Gio, GLib

from constants import APP_NAME, APP_ICON, BackendType
from core.audio_monitor import AudioMonitor
from core.camera_backend import CameraInfo
from core.camera_manager import CameraManager
from core.stream_engine import StreamEngine
from core.photo_capture import PhotoCapture
from core.video_recorder import VideoRecorder
from core.virtual_camera import VirtualCamera
from core import camera_profiles
from ui.preview_area import PreviewArea
from ui.camera_controls_page import CameraControlsPage
from ui.camera_selector import CameraSelector
from ui.photo_gallery import PhotoGallery
from ui.video_gallery import VideoGallery
from ui.settings_page import SettingsPage
from ui.effects_page import EffectsPage
from ui.immersion import ImmersionController
from ui.ip_camera_dialog import IPCameraDialog
from ui.phone_camera_dialog import PhoneCameraDialog
from ui.controllers.sidebar_ctrl import SidebarController
from ui.controllers.mobile_device_ctrl import MobileDeviceController
from core.event_bus import event_bus
from core.resource_monitor import ResourceMonitor, FeatureDescriptor
from ui.resource_warning_dialog import show_resource_warning, MONITOR_ENABLED_KEY
from utils.settings_manager import SettingsManager
from utils.async_worker import run_async
from utils.i18n import _, ngettext
from utils.media_paths import reserve_media_path
from utils import xdg
import time

log = logging.getLogger(__name__)


class BigDigicamWindow(Adw.ApplicationWindow):
    """Primary window with full-viewport preview and overlay sidebar."""

    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(1000, 650)
        self.set_size_request(700, 500)
        self.add_css_class("bigcam")

        self._closing = False
        self._selection_generation = 0
        self._capture_pending = False
        self._recording_hold = False
        self._close_hold = False
        self._settings = SettingsManager()
        self._camera_manager = CameraManager()
        self._stream_engine = StreamEngine(self._camera_manager)
        self._stream_engine.mirror = bool(self._settings.get("mirror_preview"))
        self._stream_engine.prefer_v4l2 = bool(self._settings.get("prefer-v4l2"))
        self._photo_capture = PhotoCapture(self._camera_manager)
        self._video_recorder = VideoRecorder(self._camera_manager)
        self._video_recorder.configure(
            video_codec=self._settings.get("recording-video-codec"),
            audio_codec=self._settings.get("recording-audio-codec"),
            container=self._settings.get("recording-container"),
            video_bitrate=self._settings.get("recording-video-bitrate"),
        )
        self._stream_engine._video_recorder = self._video_recorder
        self._video_recorder.connect("state-changed", self._on_recording_state)
        self._video_recorder.connect("finalized", self._on_recording_finalized)

        self._audio_monitor = AudioMonitor()
        self._tooltip_widgets: list[tuple[Gtk.Widget, str]] = []
        self._active_camera: CameraInfo | None = None
        self._known_camera_ids: set[str] = set()
        self._known_camera_names: set[str] = set()
        self._streaming_lock = threading.Lock()
        # Mobile devices are now managed by MobileDeviceController
        self._resource_monitor = ResourceMonitor()
        self._controls_cache: dict[str, list] = {}
        self._vcam_dialog_shown: set[str] = set()

        self._build_ui()
        self._setup_actions()
        self._setup_shortcuts()
        self._connect_signals()
        self._apply_theme()
        self._setup_immersion()

        self._mobile_device_ctrl = MobileDeviceController(self._camera_manager, self._immersion, self._audio_monitor)
        self._mobile_device_ctrl.phone_server.set_audio_callback(
            lambda pcm: self._video_recorder.write_audio("phone_browser", pcm))
        event_bus.connect("mobile-status-changed", self._on_mobile_status_changed)
        event_bus.connect("camera-changed", self._on_eventbus_camera_changed)
        event_bus.connect("vcam-limit-reached", self._on_vcam_limit_reached)

        # Apply initial tooltip state
        if not self._settings.get("show-help-tooltips"):
            self._set_tooltips_enabled(False)

        # Resource monitor
        self._setup_resource_monitor()

        # Initial camera detection
        ip_cameras = self._settings.get("ip_cameras")
        if ip_cameras:
            GLib.idle_add(self._camera_manager.add_ip_cameras, ip_cameras)
        GLib.idle_add(self._camera_manager.detect_cameras_async)
        GLib.idle_add(self._update_last_photo_thumbnail)

        if self._settings.get("hotplug_enabled"):
            self._camera_manager.start_hotplug()

    # -- UI build ------------------------------------------------------------

    def _register_tooltip(self, widget: Gtk.Widget, text: str) -> None:
        widget.set_tooltip_text(text)
        widget.update_property([Gtk.AccessibleProperty.LABEL], [text])
        self._tooltip_widgets.append((widget, text))

    def _update_tooltip(self, widget: Gtk.Widget, text: str) -> None:
        widget.update_property([Gtk.AccessibleProperty.LABEL], [text])
        for i, (w, _) in enumerate(self._tooltip_widgets):
            if w is widget:
                self._tooltip_widgets[i] = (widget, text)
                break
        if self._settings.get("show-help-tooltips"):
            widget.set_tooltip_text(text)
        else:
            widget.set_tooltip_text(None)

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Window-level notification banner (pushes all content down)
        self._window_banner = Adw.Banner()
        self._window_banner.set_revealed(False)
        self._window_banner_timeout: int | None = None
        root.append(self._window_banner)

        # Camera selector (created here, placed in top bar overlay)
        self._camera_selector = CameraSelector(self._camera_manager)

        # Phone camera button with icon + label + status dot overlay
        phone_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        phone_icon = Gtk.Image.new_from_icon_name("phone-symbolic")
        phone_icon.set_pixel_size(14)
        phone_label = Gtk.Label(label=_("Phone"))
        phone_box.append(phone_icon)
        phone_box.append(phone_label)

        phone_btn = Gtk.Button()
        phone_btn.set_child(phone_box)
        phone_btn.add_css_class("phone-webcam-button")
        self._register_tooltip(phone_btn, _("Use your phone as a webcam"))
        phone_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Phone as Webcam")]
        )
        phone_btn.set_action_name("win.phone-camera")
        self._phone_btn = phone_btn

        self._phone_dot = Gtk.DrawingArea()
        self._phone_dot.set_content_width(8)
        self._phone_dot.set_content_height(8)
        self._phone_dot.set_halign(Gtk.Align.END)
        self._phone_dot.set_valign(Gtk.Align.START)
        self._phone_dot.set_margin_end(4)
        self._phone_dot.set_margin_top(4)
        self._phone_dot.set_can_target(False)
        self._phone_status_color = (0.6, 0.6, 0.6)  # grey = idle
        self._phone_dot.set_draw_func(self._draw_phone_dot)
        self._phone_dot.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Phone camera status")],
        )
        self._phone_dot.set_visible(False)

        self._phone_overlay = Gtk.Overlay()
        self._phone_overlay.set_child(phone_btn)
        self._phone_overlay.add_overlay(self._phone_dot)

        # Progress bar (thin, hidden, placed as overlay later)
        self._progress = Gtk.ProgressBar(visible=False)
        self._progress.add_css_class("osd")

        # Main content: OverlaySplitView (sidebar overlays the preview)
        self._split_view = Adw.OverlaySplitView(
            show_sidebar=False,
            sidebar_position=Gtk.PackType.START,
            max_sidebar_width=400,
            min_sidebar_width=280,
            sidebar_width_fraction=0.35,
            collapsed=True,
        )

        # Content: preview area
        self._preview = PreviewArea(self._stream_engine)
        self._preview.set_show_fps(self._settings.get("show_fps"))
        self._preview.set_grid_visible(self._settings.get("grid_overlay"))
        self._preview.set_mirror(bool(self._settings.get("mirror_preview")))
        self._apply_overlay_opacity(self._settings.get("overlay-opacity"))
        self._apply_controls_opacity(self._settings.get("controls-opacity"))
        self._apply_window_opacity(self._settings.get("window-opacity"))
        self._preview.set_audio_monitor(self._audio_monitor)
        self._audio_monitor.detect_all()
        self._audio_monitor.connect("source-toggled", self._on_audio_source_toggled)
        self._audio_monitor.connect("source-volume-changed", self._on_audio_source_volume_changed)
        self._audio_monitor.connect("mute-changed", self._on_audio_mute_changed)

        # Hide PreviewArea's built-in floating toolbar (replaced by our bottom bar)
        self._preview.set_toolbar_visible(False)

        # Main overlay: preview + top/bottom bars
        self._main_overlay = Gtk.Overlay()
        self._main_overlay.set_overflow(Gtk.Overflow.HIDDEN)

        # Black background box behind the preview (visible during transitions)
        video_bg = Gtk.Box()
        video_bg.add_css_class("video-bg")
        video_bg.set_hexpand(True)
        video_bg.set_vexpand(True)
        self._main_overlay.set_child(video_bg)

        # Preview as overlay on top of the black background
        self._main_overlay.add_overlay(self._preview)
        self._main_overlay.set_measure_overlay(self._preview, False)
        self._main_overlay.set_clip_overlay(self._preview, True)

        # Flash overlay (white, shown briefly on photo capture)
        self._flash_overlay = Gtk.Box()
        self._flash_overlay.add_css_class("flash-overlay")
        self._flash_overlay.set_hexpand(True)
        self._flash_overlay.set_vexpand(True)
        self._flash_overlay.set_opacity(0)
        self._flash_overlay.set_can_target(False)
        self._main_overlay.add_overlay(self._flash_overlay)

        # -- Top bar overlay --------------------------------------------------
        top_bar = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
        )
        top_bar.add_css_class("osd-bar")
        top_bar.add_css_class("top-bar")

        # Window controls (minimize/maximize/close) on the right end
        win_controls_end = Gtk.WindowControls(side=Gtk.PackType.END)
        win_controls_end.set_halign(Gtk.Align.END)

        # Sidebar toggle button (Controls, top-left)
        controls_btn = Gtk.Button()
        controls_icon = Gtk.Image.new_from_icon_name("sidebar-show-symbolic")
        controls_icon.set_pixel_size(20)
        controls_btn.set_child(controls_icon)
        self._register_tooltip(controls_btn, _("Controls"))
        controls_btn.add_css_class("controls-toggle-btn")
        controls_btn.connect("clicked", self._on_sidebar_toggle_clicked)
        top_bar.append(controls_btn)

        # Always on Top pin button (before camera selector)
        pin_btn = Gtk.ToggleButton()
        pin_icon = Gtk.Image.new_from_icon_name("view-pin-symbolic")
        pin_icon.set_pixel_size(16)
        pin_btn.set_child(pin_icon)
        self._register_tooltip(pin_btn, _("Always on Top"))
        pin_btn.add_css_class("pin-btn")
        pin_btn.connect("toggled", self._on_always_on_top_toggled)
        self._pin_btn = pin_btn
        pin_btn.set_visible(False)  # No portable GTK4/Wayland keep-above protocol.
        top_bar.append(pin_btn)

        # Phone button (left of camera selector)
        top_bar.append(self._phone_overlay)

        # Spacer (push camera selector to center)
        top_spacer_start = Gtk.Box()
        top_spacer_start.set_hexpand(True)
        top_bar.append(top_spacer_start)

        # Camera selector (centered)
        top_bar.append(self._camera_selector)

        # Spacer (push right items to the end)
        top_spacer_end = Gtk.Box()
        top_spacer_end.set_hexpand(True)
        top_bar.append(top_spacer_end)

        # Recording timer (center, visible only during recording)
        rec_timer_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4,
            halign=Gtk.Align.CENTER,
        )
        rec_timer_box.add_css_class("rec-timer")
        rec_indicator = Gtk.Box()
        rec_indicator.add_css_class("rec-indicator")
        rec_indicator.set_halign(Gtk.Align.CENTER)
        rec_indicator.set_valign(Gtk.Align.CENTER)
        rec_timer_box.append(rec_indicator)
        rec_timer_label = Gtk.Label(label="00:00")
        rec_timer_box.append(rec_timer_label)
        rec_timer_box.set_visible(False)
        self._rec_timer_box = rec_timer_box
        self._rec_timer_label = rec_timer_label
        self._rec_timer_seconds = 0
        self._rec_timer_source_id: int | None = None
        top_bar.append(rec_timer_box)

        # Refresh button
        top_refresh = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self._register_tooltip(top_refresh, _("Refresh cameras"))
        top_refresh.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Refresh camera list")]
        )
        top_refresh.add_css_class("flat")
        top_refresh.add_css_class("circular")
        top_refresh.set_action_name("win.refresh")
        top_bar.append(top_refresh)

        # Menu button
        top_menu_btn = Gtk.MenuButton()
        top_menu_btn.set_icon_name("open-menu-symbolic")
        self._register_tooltip(top_menu_btn, _("Menu"))
        top_menu_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Main menu")]
        )
        top_menu_btn.add_css_class("flat")
        top_menu_btn.add_css_class("circular")
        top_menu_btn.set_menu_model(self._build_menu())
        top_menu_btn.connect("notify::active", self._on_menu_popover_toggled)
        top_bar.append(top_menu_btn)

        # Window controls (CSD buttons)
        top_bar.append(win_controls_end)
        # Wrap top bar in WindowHandle for CSD drag + window controls
        top_handle = Gtk.WindowHandle()
        top_handle.set_child(top_bar)

        self._top_bar_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.CROSSFADE,
            transition_duration=300,
            reveal_child=True,
        )
        self._top_bar_revealer.set_child(top_handle)
        self._top_bar_revealer.set_halign(Gtk.Align.FILL)
        self._top_bar_revealer.set_valign(Gtk.Align.START)
        self._main_overlay.add_overlay(self._top_bar_revealer)

        # -- Bottom bar overlay -----------------------------------------------
        bottom_zone = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
        )
        bottom_zone.add_css_class("osd-bar")
        bottom_zone.add_css_class("bottom-bar")

        # -- Mode Switcher (Photo / Video icon toggle) ------------------------
        self._current_mode = "photo"
        mode_switcher = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=0,
            halign=Gtk.Align.CENTER,
        )
        mode_switcher.add_css_class("mode-switcher")

        photo_btn = Gtk.ToggleButton()
        photo_btn.set_icon_name("camera-photo-symbolic")
        photo_btn.set_active(True)
        self._register_tooltip(photo_btn, _("Photo mode"))
        photo_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Photo mode")]
        )
        self._mode_photo_btn = photo_btn

        video_btn = Gtk.ToggleButton()
        video_btn.set_icon_name("emblem-videos-symbolic")
        video_btn.set_group(photo_btn)
        self._register_tooltip(video_btn, _("Video mode"))
        video_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Video mode")]
        )
        self._mode_video_btn = video_btn

        photo_btn.connect("toggled", self._on_mode_toggled, "photo")
        video_btn.connect("toggled", self._on_mode_toggled, "video")

        mode_switcher.append(photo_btn)
        mode_switcher.append(video_btn)
        bottom_zone.append(mode_switcher)

        # -- Controls bar (CenterBox) ----------------------------------------
        controls_bar = Gtk.CenterBox()
        controls_bar.set_halign(Gtk.Align.FILL)
        controls_bar.add_css_class("controls-bar")

        # Left: last photo thumbnail + mirror button
        controls_start = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        # Last photo thumbnail (perfect circle, clipped)
        last_photo_btn = Gtk.Button()
        last_photo_btn.add_css_class("last-photo-btn")
        last_photo_btn.add_css_class("circular")
        last_photo_btn.set_size_request(44, 44)
        last_photo_btn.set_halign(Gtk.Align.CENTER)
        last_photo_btn.set_valign(Gtk.Align.CENTER)
        last_photo_btn.set_overflow(Gtk.Overflow.HIDDEN)
        self._register_tooltip(last_photo_btn, _("Last photo"))
        last_photo_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Open last photo")]
        )
        last_photo_btn.connect("clicked", self._on_last_photo_clicked)
        last_photo_btn.set_visible(False)
        self._last_photo_btn = last_photo_btn
        controls_start.append(last_photo_btn)

        mirror_btn = Gtk.ToggleButton()
        mirror_icon = Gtk.Image.new_from_icon_name("object-flip-horizontal-symbolic")
        mirror_icon.set_pixel_size(20)
        mirror_btn.set_child(mirror_icon)
        self._register_tooltip(mirror_btn, _("Mirror preview"))
        mirror_btn.add_css_class("bottom-circle-btn")
        mirror_btn.set_size_request(44, 44)
        mirror_btn.set_halign(Gtk.Align.CENTER)
        mirror_btn.set_valign(Gtk.Align.CENTER)
        mirror_btn.set_active(bool(self._settings.get("mirror_preview")))
        self._mirror_btn_handler_id = mirror_btn.connect("toggled", self._on_mirror_btn_toggled)
        self._mirror_btn = mirror_btn
        controls_start.append(mirror_btn)

        # QR Code scanner toggle
        qr_btn = Gtk.ToggleButton()
        qr_icon = Gtk.Image.new_from_icon_name("scanner-symbolic")
        qr_icon.set_pixel_size(20)
        qr_btn.set_child(qr_icon)
        self._register_tooltip(qr_btn, _("Scan QR Codes"))
        qr_btn.add_css_class("bottom-circle-btn")
        qr_btn.set_size_request(44, 44)
        qr_btn.set_halign(Gtk.Align.CENTER)
        qr_btn.set_valign(Gtk.Align.CENTER)
        self._qr_btn_handler_id = qr_btn.connect("toggled", self._on_qr_quick_toggled)
        self._qr_quick_btn = qr_btn
        controls_start.append(qr_btn)

        # Virtual Camera toggle
        vcam_btn = Gtk.ToggleButton()
        vcam_overlay = Gtk.Overlay()
        vcam_icon = Gtk.Image.new_from_icon_name("camera-web-symbolic")
        vcam_icon.set_pixel_size(20)
        vcam_overlay.set_child(vcam_icon)

        self._vcam_badge = Gtk.Label(label="0")
        self._vcam_badge.add_css_class("vcam-badge")
        self._vcam_badge.set_halign(Gtk.Align.END)
        self._vcam_badge.set_valign(Gtk.Align.START)
        self._vcam_badge.set_margin_top(2)
        self._vcam_badge.set_margin_end(2)
        self._vcam_badge.set_visible(False)
        vcam_overlay.add_overlay(self._vcam_badge)

        vcam_btn.set_child(vcam_overlay)
        self._register_tooltip(vcam_btn, _("Enable Virtual Camera"))
        vcam_btn.add_css_class("bottom-circle-btn")
        vcam_btn.set_size_request(44, 44)
        vcam_btn.set_halign(Gtk.Align.CENTER)
        vcam_btn.set_valign(Gtk.Align.CENTER)
        vcam_btn.set_active(bool(self._settings.get("virtual-camera-enabled")))
        self._vcam_btn_handler_id = vcam_btn.connect("toggled", self._on_vcam_quick_toggled)
        self._vcam_quick_btn = vcam_btn
        controls_start.append(vcam_btn)

        controls_bar.set_start_widget(controls_start)

        # Center: capture + record buttons
        controls_center = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        controls_center.set_halign(Gtk.Align.CENTER)

        capture_btn = Gtk.Button.new_from_icon_name("camera-photo-symbolic")
        capture_btn.add_css_class("circular")
        capture_btn.add_css_class("capture-button")
        self._register_tooltip(capture_btn, _("Capture photo"))
        capture_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Capture photo")]
        )
        capture_btn.set_action_name("win.capture")
        self._bottom_capture_btn = capture_btn
        controls_center.append(capture_btn)

        # Record button (red dot style — visible only in video mode)
        rec_dot = Gtk.Box()
        rec_dot.add_css_class("rec-dot")
        rec_stop = Gtk.Box()
        rec_stop.add_css_class("rec-stop-icon")
        rec_stop.set_visible(False)
        rec_stop.set_halign(Gtk.Align.CENTER)
        rec_stop.set_valign(Gtk.Align.CENTER)
        rec_overlay = Gtk.Overlay()
        rec_overlay.set_child(rec_dot)
        rec_overlay.add_overlay(rec_stop)
        rec_overlay.set_halign(Gtk.Align.CENTER)
        rec_overlay.set_valign(Gtk.Align.CENTER)
        record_btn = Gtk.Button()
        record_btn.set_child(rec_overlay)
        record_btn.add_css_class("record-button")
        record_btn.set_size_request(44, 44)
        record_btn.set_halign(Gtk.Align.CENTER)
        record_btn.set_valign(Gtk.Align.CENTER)
        self._register_tooltip(record_btn, _("Record video (Ctrl+R)"))
        record_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Record video")]
        )
        record_btn.set_action_name("win.record-toggle")
        record_btn.set_visible(False)  # Hidden in photo mode
        self._bottom_record_btn = record_btn
        self._bottom_rec_dot = rec_dot
        self._bottom_rec_stop = rec_stop
        controls_center.append(record_btn)

        controls_bar.set_center_widget(controls_center)

        # Right: zoom + sidebar toggle
        controls_end = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        zoom_btn = Gtk.Button(label="1x")
        zoom_btn.add_css_class("zoom-btn")
        zoom_btn.set_size_request(44, 44)
        zoom_btn.set_halign(Gtk.Align.CENTER)
        zoom_btn.set_valign(Gtk.Align.CENTER)
        self._register_tooltip(zoom_btn, _("Zoom level"))
        zoom_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Change zoom level")]
        )
        zoom_btn.connect("clicked", self._on_zoom_btn_clicked)
        self._zoom_btn = zoom_btn
        self._zoom_levels = [1.0, 1.5, 2.0]
        self._zoom_index = 0
        controls_end.append(zoom_btn)

        # Grid toggle
        grid_btn = Gtk.ToggleButton()
        grid_icon = Gtk.Image.new_from_icon_name("view-grid-symbolic")
        grid_icon.set_pixel_size(20)
        grid_btn.set_child(grid_icon)
        self._register_tooltip(grid_btn, _("Toggle grid overlay"))
        grid_btn.add_css_class("bottom-circle-btn")
        grid_btn.set_size_request(44, 44)
        grid_btn.set_halign(Gtk.Align.CENTER)
        grid_btn.set_valign(Gtk.Align.CENTER)
        grid_btn.set_active(self._settings.get("grid_overlay"))
        grid_btn.connect("toggled", self._on_grid_btn_toggled)
        self._grid_btn = grid_btn
        controls_end.append(grid_btn)

        # Timer cycle button
        timer_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        timer_icon = Gtk.Image.new_from_icon_name("timer-symbolic")
        timer_icon.set_pixel_size(20)
        saved_timer = self._settings.get("capture-timer") or 0
        if saved_timer > 0:
            timer_init_label = f"{saved_timer}s"
        else:
            timer_init_label = _("Off")
        self._timer_label = Gtk.Label(label=timer_init_label)
        self._timer_label.add_css_class("caption")
        timer_box.append(timer_icon)
        timer_box.append(self._timer_label)
        timer_btn = Gtk.Button()
        timer_btn.set_child(timer_box)
        self._register_tooltip(timer_btn, _("Capture timer"))
        timer_btn.add_css_class("bottom-circle-btn")
        timer_btn.set_size_request(44, 44)
        timer_btn.set_halign(Gtk.Align.CENTER)
        timer_btn.set_valign(Gtk.Align.CENTER)
        if saved_timer > 0:
            timer_btn.add_css_class("timer-active")
        timer_btn.set_action_name("win.cycle-timer")
        self._timer_btn = timer_btn
        controls_end.append(timer_btn)

        fullscreen_btn = Gtk.Button()
        fs_icon = Gtk.Image.new_from_icon_name("view-fullscreen-symbolic")
        fs_icon.set_pixel_size(20)
        fullscreen_btn.set_child(fs_icon)
        self._register_tooltip(fullscreen_btn, _("Fullscreen (F11)"))
        fullscreen_btn.add_css_class("bottom-circle-btn")
        fullscreen_btn.set_size_request(44, 44)
        fullscreen_btn.set_halign(Gtk.Align.CENTER)
        fullscreen_btn.set_valign(Gtk.Align.CENTER)
        fullscreen_btn.connect("clicked", lambda _b: self._on_toggle_fullscreen_action())
        controls_end.append(fullscreen_btn)

        controls_bar.set_end_widget(controls_end)

        bottom_zone.append(controls_bar)

        self._bottom_bar_revealer = Gtk.Revealer(
            transition_type=Gtk.RevealerTransitionType.CROSSFADE,
            transition_duration=300,
            reveal_child=True,
        )
        self._bottom_bar_revealer.set_child(bottom_zone)
        self._bottom_bar_revealer.set_halign(Gtk.Align.FILL)
        self._bottom_bar_revealer.set_valign(Gtk.Align.END)
        self._main_overlay.add_overlay(self._bottom_bar_revealer)

        self._split_view.set_content(self._main_overlay)


        # Controls page
        self._controls_page = CameraControlsPage(self._camera_manager, self._stream_engine)

        # Effects page
        self._effects_page = EffectsPage(self._stream_engine.effects)

        # Photo gallery page
        self._gallery = PhotoGallery()

        # Video gallery page
        self._video_gallery = VideoGallery()

        # Settings page (includes Tools and Virtual Camera)
        self._settings_page = SettingsPage(self._settings, self._stream_engine, self._camera_manager)
        self._settings_page.connect("qr-detected", self._on_qr_detected)
        self._settings_page.connect(
            "virtual-camera-toggled", self._on_virtual_camera_toggled
        )
        self._settings_page.connect(
            "virtual-camera-device-toggled", self._on_virtual_camera_device_toggled
        )
        self._settings_page.connect("resolution-changed", self._on_resolution_changed)
        self._settings_page.connect("fps-limit-changed", self._on_fps_limit_changed)
        self._settings_page.connect(
            "grid-overlay-changed", self._on_grid_overlay_changed
        )
        self._settings_page.connect(
            "overlay-opacity-changed", self._on_overlay_opacity_changed
        )
        self._settings_page.connect(
            "controls-opacity-changed", self._on_controls_opacity_changed
        )
        self._settings_page.connect(
            "window-opacity-changed", self._on_window_opacity_changed
        )
        # Restore virtual camera enabled state from settings
        if self._settings.get("virtual-camera-enabled"):
            VirtualCamera.set_enabled(True)
            self._settings_page.set_vc_toggle_active(True)
        VirtualCamera.set_max_devices(self._settings.get("vcam-max-devices"))
        VirtualCamera.set_name_template(self._settings.get("vcam-name-template"))
        # Clean stale v4l2loopback devices from previous sessions
        VirtualCamera.cleanup_dynamic_devices()

        stack_pages = {
            "controls": (self._controls_page, _("Controls"), "adjustlevels"),
            "effects": (self._effects_page, _("Effects"), "draw-watercolor"),
            "gallery": (self._gallery, _("Photos"), "view-list-images"),
            "videos": (self._video_gallery, _("Videos"), "view-list-video"),
            "settings": (self._settings_page, _("Settings"), "configure"),
        }
        self._sidebar_ctrl = SidebarController(self._split_view, stack_pages)
        self._view_stack = self._sidebar_ctrl.stack

        # React to sidebar visibility for immersion
        self._split_view.connect("notify::show-sidebar", self._on_sidebar_toggled)

        root.append(self._split_view)

        self._root_box = root
        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_child(root)
        self.set_content(self._toast_overlay)

    def _on_sidebar_toggle_clicked(self, _btn: Gtk.Button) -> None:
        visible = self._split_view.get_show_sidebar()
        self._split_view.set_show_sidebar(not visible)

    def _on_sidebar_tab_toggled(self, btn: Gtk.ToggleButton, page_name: str) -> None:
        if btn.get_active():
            self._view_stack.set_visible_child_name(page_name)

    def _on_mode_toggled(self, btn: Gtk.ToggleButton, mode: str) -> None:
        """Switch between Photo and Video mode."""
        if not btn.get_active():
            return
        self._current_mode = mode
        if mode == "video":
            self._bottom_capture_btn.set_icon_name("media-record-symbolic")
            self._bottom_capture_btn.add_css_class("video-mode")
            self._update_tooltip(self._bottom_capture_btn, _("Start recording"))
            self._bottom_record_btn.set_visible(False)
        else:
            self._bottom_capture_btn.set_icon_name("camera-photo-symbolic")
            self._bottom_capture_btn.remove_css_class("video-mode")
            self._bottom_capture_btn.remove_css_class("recording")
            self._update_tooltip(self._bottom_capture_btn, _("Capture photo"))
            self._bottom_record_btn.set_visible(False)
        self._update_last_media_thumbnail()

    def _on_zoom_btn_clicked(self, _btn: Gtk.Button) -> None:
        """Cycle through zoom levels: 1x → 1.5x → 2x → 1x."""
        self._zoom_index = (self._zoom_index + 1) % len(self._zoom_levels)
        level = self._zoom_levels[self._zoom_index]
        label = f"{level:.0f}x" if level == int(level) else f"{level}x"
        self._zoom_btn.set_label(label)
        self._stream_engine.set_zoom(level)

    def _on_last_photo_clicked(self, _button):
        path = getattr(self, "_last_media_path", None)
        if path:
            Gtk.FileLauncher.new(Gio.File.new_for_path(path)).launch(self, None, None, None)

    def _update_last_media_thumbnail(self, specific_path=None):
        if self._closing:
            return GLib.SOURCE_REMOVE
        from core.media_library import scan, thumbnail, MediaEntry, PHOTO_EXTS, VIDEO_EXTS
        mode = self._current_mode
        self._thumbnail_generation = getattr(self, "_thumbnail_generation", 0) + 1
        generation = self._thumbnail_generation
        def prepare():
            if specific_path:
                st = os.stat(specific_path, follow_symlinks=False)
                entry = MediaEntry(specific_path, st.st_size, st.st_mtime, st.st_mtime_ns, mode == "video")
            else:
                entries = scan(xdg.videos_dir() if mode == "video" else xdg.photos_dir(),
                               VIDEO_EXTS if mode == "video" else PHOTO_EXTS)
                if not entries:
                    return None
                entry = entries[0]
            return entry.path, thumbnail(entry)
        def done(result):
            if self._closing or generation != self._thumbnail_generation or mode != self._current_mode:
                return
            self._last_media_path = result[0] if result else None
            self._last_photo_btn.set_visible(bool(result))
            self._update_tooltip(self._last_photo_btn, _("Last video") if mode == "video" else _("Last photo"))
            if result:
                picture = Gtk.Picture.new_for_filename(result[1])
                picture.set_content_fit(Gtk.ContentFit.COVER)
                picture.set_size_request(40, 40)
                self._last_photo_btn.set_child(picture)
        run_async(prepare, on_success=done, on_error=lambda exc: done(None))
        return GLib.SOURCE_REMOVE

    def _set_video_thumbnail(self, thumb_path: str) -> None:
        try:
            from gi.repository import GdkPixbuf
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(thumb_path, 40, 40, True)
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            image = Gtk.Image.new_from_paintable(texture)
            image.set_pixel_size(40)
            self._last_photo_btn.set_child(image)
            self._last_photo_btn.set_visible(True)
        except Exception:
            icon = Gtk.Image.new_from_icon_name("video-x-generic-symbolic")
            icon.set_pixel_size(24)
            self._last_photo_btn.set_child(icon)
            self._last_photo_btn.set_visible(True)

    def _update_last_photo_thumbnail(self) -> None:
        """Refresh thumbnail — delegates to mode-aware method."""
        self._update_last_media_thumbnail()

    def _get_last_photo_path(self) -> str | None:
        """Return the path of the most recently captured photo, or None."""
        from utils import xdg
        photos_dir = xdg.photos_dir()
        if not os.path.isdir(photos_dir):
            return None
        entries = []
        for entry in os.scandir(photos_dir):
            if entry.is_file() and entry.name.lower().endswith(
                (".jpg", ".jpeg", ".png", ".webp")
            ):
                entries.append(entry)
        if not entries:
            return None
        entries.sort(key=lambda e: e.stat().st_mtime, reverse=True)
        return entries[0].path

    def _get_last_video_path(self) -> str | None:
        """Return the path of the most recently recorded video, or None."""
        from utils import xdg
        vids_dir = xdg.videos_dir()
        if not os.path.isdir(vids_dir):
            return None
        entries = []
        for entry in os.scandir(vids_dir):
            if entry.is_file() and entry.name.lower().endswith(
                (".mp4", ".mkv", ".webm", ".avi")
            ):
                entries.append(entry)
        if not entries:
            return None
        entries.sort(key=lambda e: e.stat().st_mtime, reverse=True)
        return entries[0].path

    def _on_sidebar_drag(self, gesture: Gtk.GestureDrag, offset_x: float, _offset_y: float) -> None:
        """Resize the sidebar by dragging the handle."""
        current_width = self._split_view.get_max_sidebar_width()
        new_width = max(280, min(500, current_width + offset_x))
        self._split_view.set_max_sidebar_width(new_width)

    def _on_sidebar_toggled(self, split_view: Adw.OverlaySplitView, _pspec: object) -> None:
        if split_view.get_show_sidebar():
            self._immersion.inhibit()
        else:
            self._immersion.uninhibit()

    def _on_menu_popover_toggled(self, btn: Gtk.MenuButton, _pspec: object) -> None:
        if btn.get_active():
            self._immersion.inhibit()
        else:
            self._immersion.uninhibit()

    def _on_grid_btn_toggled(self, btn: Gtk.ToggleButton) -> None:
        visible = btn.get_active()
        self._preview.set_grid_visible(visible)
        self._settings.set("grid_overlay", visible)
        if self._settings_page._grid_row.get_active() != visible:
            self._settings_page._grid_row.set_active(visible)

    def _on_mirror_btn_toggled(self, btn: Gtk.ToggleButton) -> None:
        new_val = btn.get_active()
        self._settings.set("mirror_preview", new_val)
        self._stream_engine.mirror = new_val
        self._preview.set_mirror(new_val)
        self._settings_page.handler_block(self._mirror_settings_handler_id)
        self._settings_page._mirror_row.set_active(new_val)
        self._settings_page.handler_unblock(self._mirror_settings_handler_id)

    def _on_mirror_btn_clicked(self, _btn: Gtk.Button) -> None:
        self._mirror_btn.set_active(not self._mirror_btn.get_active())

    def _on_qr_quick_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._settings_page._qr_row.handler_block(self._qr_settings_handler_id)
        self._settings_page._qr_row.set_active(btn.get_active())
        self._settings_page._qr_row.handler_unblock(self._qr_settings_handler_id)

    def _on_vcam_quick_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._settings_page._vc_toggle_row.handler_block(self._vcam_settings_handler_id)
        self._settings_page._vc_toggle_row.set_active(btn.get_active())
        self._settings_page._vc_toggle_row.handler_unblock(self._vcam_settings_handler_id)

    def _on_settings_qr_changed(self, row: object, _pspec: object) -> None:
        self._qr_quick_btn.handler_block(self._qr_btn_handler_id)
        self._qr_quick_btn.set_active(row.get_active())
        self._qr_quick_btn.handler_unblock(self._qr_btn_handler_id)

    def _on_settings_vcam_changed(self, row: object, _pspec: object) -> None:
        self._vcam_quick_btn.handler_block(self._vcam_btn_handler_id)
        self._vcam_quick_btn.set_active(row.get_active())
        self._vcam_quick_btn.handler_unblock(self._vcam_btn_handler_id)
        self._update_vcam_badge()

    def _update_vcam_badge(self):
        count = len(VirtualCamera._allocations) if VirtualCamera.is_enabled() else 0
        self._vcam_badge.set_label(str(count))
        self._vcam_badge.set_visible(count > 0)
        self._vcam_badge.update_property([Gtk.AccessibleProperty.LABEL],
            [ngettext("%d virtual camera allocated", "%d virtual cameras allocated", count) % count])

    def _on_help_tooltips_changed(self, _page: object, enabled: bool) -> None:
        self._set_tooltips_enabled(enabled)

    def _on_capture_timer_changed(self, _page: object, value: int) -> None:
        if value == 0:
            self._timer_label.set_label(_("Off"))
            self._timer_btn.remove_css_class("timer-active")
            self._update_tooltip(self._timer_btn, _("Capture timer: Off"))
        else:
            self._timer_label.set_label(f"{value}s")
            self._timer_btn.add_css_class("timer-active")
            self._update_tooltip(self._timer_btn, _("Capture timer: %ds") % value)

    def _on_recording_config_changed(self, _page: object) -> None:
        self._video_recorder.configure(
            video_codec=self._settings.get("recording-video-codec"),
            audio_codec=self._settings.get("recording-audio-codec"),
            container=self._settings.get("recording-container"),
            video_bitrate=self._settings.get("recording-video-bitrate"),
        )

    def _set_tooltips_enabled(self, enabled: bool) -> None:
        for widget, tooltip_text in self._tooltip_widgets:
            widget.set_tooltip_text(tooltip_text if enabled else None)

    # -- action handlers for keyboard shortcuts ------------------------------

    def _on_toggle_mirror_action(self, *_args) -> None:
        self._on_mirror_btn_clicked(None)

    def _on_toggle_grid_action(self, *_args) -> None:
        self._grid_btn.set_active(not self._grid_btn.get_active())

    def _on_cycle_timer_action(self, *_args) -> None:
        self._cycle_timer()

    def _on_toggle_fullscreen_action(self, *_args) -> None:
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def _on_escape_action(self, *_args):
        if self._preview.is_countdown_active():
            self._preview.cancel_countdown()
            self._show_notification(_("Capture cancelled."))
        elif self._split_view.get_show_sidebar():
            self._split_view.set_show_sidebar(False)
        elif self.is_fullscreen():
            self.unfullscreen()

    def _on_always_on_top_toggled(self, btn):
        self._show_notification(_("Use your window manager to keep this window above others."))

    def _on_show_welcome_action(self, *_args) -> None:
        from ui.welcome_dialog import WelcomeDialog
        dialog = WelcomeDialog(self, self._settings)
        self._immersion.inhibit()
        dialog._dialog.connect("closed", lambda *_: self._immersion.uninhibit())
        dialog.present()

    def _is_editing_text(self):
        focus = self.get_focus()
        while focus is not None:
            if isinstance(focus, (Gtk.Editable, Gtk.TextView)):
                return True
            focus = focus.get_parent()
        return False

    def _set_zoom_level(self, index: int) -> None:
        if self._is_editing_text():
            return
        self._zoom_index = index % len(self._zoom_levels)
        level = self._zoom_levels[self._zoom_index]
        label = f"{level:.0f}x" if level == int(level) else f"{level}x"
        self._zoom_btn.set_label(label)
        self._stream_engine.set_zoom(level)

    def _switch_sidebar_tab(self, index: int) -> None:
        if self._is_editing_text():
            return
        pages = self._view_stack.get_pages()
        if index < pages.get_n_items():
            page = pages.get_item(index)
            self._view_stack.set_visible_child_name(page.get_name())
            if not self._split_view.get_show_sidebar():
                self._split_view.set_show_sidebar(True)

    def _cycle_timer(self) -> None:
        """Cycle capture timer: Off → 3s → 5s → 10s → Off."""
        _TIMER_VALUES = [0, 3, 5, 10]
        current = self._settings.get("capture-timer") or 0
        try:
            idx = _TIMER_VALUES.index(current)
        except ValueError:
            idx = 0
        next_val = _TIMER_VALUES[(idx + 1) % len(_TIMER_VALUES)]
        self._settings.set("capture-timer", next_val)
        if next_val == 0:
            self._timer_label.set_label(_("Off"))
            self._timer_btn.remove_css_class("timer-active")
            self._update_tooltip(self._timer_btn, _("Capture timer: Off"))
        else:
            self._timer_label.set_label(f"{next_val}s")
            self._timer_btn.add_css_class("timer-active")
            self._update_tooltip(self._timer_btn, _("Capture timer: %ds") % next_val)
        # Sync settings page ComboRow
        new_idx = _TIMER_VALUES.index(next_val)
        self._settings_page._syncing_timer = True
        self._settings_page._timer_row.set_selected(new_idx)
        self._settings_page._syncing_timer = False

    def _trigger_flash(self) -> None:
        """Show a brief white flash overlay on photo capture."""
        if self._settings.get("reduce-motion") or not Gtk.Settings.get_default().get_property("gtk-enable-animations"):
            return
        self._flash_overlay.set_opacity(0.8)
        GLib.timeout_add(100, self._flash_fade_out)

    def _show_notification(self, message: str, _level: str = "info", timeout_ms: int = 3000, **_kwargs) -> None:
        """Show a window-level banner that pushes content down."""
        if self._window_banner_timeout is not None:
            GLib.source_remove(self._window_banner_timeout)
            self._window_banner_timeout = None
        self._window_banner.set_title(message)
        if hasattr(self._window_banner, "announce"):
            self._window_banner.announce(message, Gtk.AccessibleAnnouncementPriority.MEDIUM)
        self._window_banner.set_revealed(True)
        if timeout_ms > 0:
            self._window_banner_timeout = GLib.timeout_add(
                timeout_ms, self._dismiss_notification
            )

    def _dismiss_notification(self) -> bool:
        self._window_banner_timeout = None
        self._window_banner.set_revealed(False)
        return GLib.SOURCE_REMOVE

    def _flash_fade_out(self) -> bool:
        self._flash_overlay.set_opacity(0)
        return GLib.SOURCE_REMOVE

    def _start_rec_timer(self) -> None:
        """Start the recording duration timer in the top bar."""
        self._rec_started_at = time.monotonic()
        self._rec_timer_seconds = 0
        self._rec_timer_label.set_label("00:00")
        self._rec_timer_box.set_visible(True)
        self._rec_timer_source_id = GLib.timeout_add_seconds(1, self._update_rec_timer)

    def _stop_rec_timer(self) -> None:
        """Stop and hide the recording timer."""
        if self._rec_timer_source_id is not None:
            GLib.source_remove(self._rec_timer_source_id)
            self._rec_timer_source_id = None
        self._rec_timer_box.set_visible(False)
        self._rec_timer_seconds = 0

    def _update_rec_timer(self) -> bool:
        self._rec_timer_seconds = int(time.monotonic() - self._rec_started_at)
        mins, secs = divmod(self._rec_timer_seconds, 60)
        self._rec_timer_label.set_label(f"{mins:02d}:{secs:02d}")
        return GLib.SOURCE_CONTINUE

    def _build_menu(self) -> Gio.Menu:
        menu = Gio.Menu()
        section1 = Gio.Menu()
        section1.append(_("Capture Photo") + " (Ctrl+P)", "win.capture")
        section1.append(_("Record Video") + " (Ctrl+R)", "win.record-toggle")
        menu.append_section(None, section1)

        section2 = Gio.Menu()
        section2.append(_("Save Profile") + " (Ctrl+S)", "win.save-profile")
        section2.append(_("Load Profile") + " (Ctrl+L)", "win.load-profile")
        menu.append_section(_("Profiles"), section2)

        section3 = Gio.Menu()
        section3.append(_("Add IP Camera…"), "win.add-ip")
        section3.append(_("Phone as Webcam…"), "win.phone-camera")
        section3.append(_("Refresh") + " (F5)", "win.refresh")
        section3.append(_("Welcome Screen"), "win.show-welcome")
        section3.append(_("About"), "win.about")
        menu.append_section(None, section3)

        section_quit = Gio.Menu()
        section_quit.append(_("Quit") + " (Ctrl+Q)", "app.quit")
        menu.append_section(None, section_quit)
        return menu

    # -- actions -------------------------------------------------------------

    def _setup_actions(self) -> None:
        simple_actions = {
            "refresh": self._on_refresh,
            "add-ip": self._on_add_ip,
            "phone-camera": self._on_phone_camera,
            "about": self._on_about,
            "capture": self._on_capture_action,
            "record-toggle": self._on_record_toggle,
            "save-profile": self._on_save_profile,
            "load-profile": self._on_load_profile,
            "toggle-mirror": self._on_toggle_mirror_action,
            "toggle-grid": self._on_toggle_grid_action,
            "cycle-timer": self._on_cycle_timer_action,
            "toggle-fullscreen": self._on_toggle_fullscreen_action,
            "toggle-sidebar": lambda *_a: (
                self._on_sidebar_toggle_clicked(None)
                if not self._is_editing_text() else None
            ),
            "zoom-1x": lambda *_a: self._set_zoom_level(0),
            "zoom-1.5x": lambda *_a: self._set_zoom_level(1),
            "zoom-2x": lambda *_a: self._set_zoom_level(2),
            "escape": self._on_escape_action,
            "switch-tab-1": lambda *_a: self._switch_sidebar_tab(0),
            "switch-tab-2": lambda *_a: self._switch_sidebar_tab(1),
            "switch-tab-3": lambda *_a: self._switch_sidebar_tab(2),
            "switch-tab-4": lambda *_a: self._switch_sidebar_tab(3),
            "switch-tab-5": lambda *_a: self._switch_sidebar_tab(4),
            "show-welcome": self._on_show_welcome_action,
        }
        for name, callback in simple_actions.items():
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)

    def _setup_shortcuts(self) -> None:
        app = self.get_application()
        if app is None:
            return
        shortcuts = {
            "win.capture": ["<Primary>p"],
            "win.record-toggle": ["<Primary>r"],
            "win.refresh": ["F5", "<Primary>F5"],
            "win.save-profile": ["<Primary>s"],
            "win.load-profile": ["<Primary>l"],
            "win.toggle-mirror": ["<Primary>m"],
            "win.toggle-grid": ["<Primary>g"],
            "win.cycle-timer": ["<Primary>t"],
            "win.toggle-fullscreen": ["F11"],
            "win.toggle-sidebar": ["F9"],
            "win.zoom-1x": ["<Alt>1"],
            "win.zoom-1.5x": ["<Alt>2"],
            "win.zoom-2x": ["<Alt>3"],
            "win.escape": ["Escape"],
            "win.switch-tab-1": ["<Primary>1"],
            "win.switch-tab-2": ["<Primary>2"],
            "win.switch-tab-3": ["<Primary>3"],
            "win.switch-tab-4": ["<Primary>4"],
            "win.switch-tab-5": ["<Primary>5"],
            "app.quit": ["<Primary>q"],
        }
        for action_name, accels in shortcuts.items():
            app.set_accels_for_action(action_name, accels)

    # -- signals -------------------------------------------------------------

    def _connect_signals(self) -> None:
        self._camera_selector.connect("camera-selected", self._on_camera_selected)
        self._preview.connect("capture-requested", self._on_capture)
        self._preview.connect("record-toggled", lambda _p: self._on_record_toggle())
        self._preview.connect("retry-requested", self._on_retry)
        self._camera_manager.connect("camera-error", self._on_camera_error)
        self._camera_manager.connect(
            "cameras-changed", self._on_cameras_changed_auto_start
        )
        self._stream_engine.connect("device-busy", self._on_device_busy)
        self._settings_page.connect("show-fps-changed", self._on_show_fps_changed)
        self._mirror_settings_handler_id = self._settings_page.connect("mirror-changed", self._on_mirror_changed)
        self._qr_settings_handler_id = self._settings_page._qr_row.connect("notify::active", self._on_settings_qr_changed)
        self._vcam_settings_handler_id = self._settings_page._vc_toggle_row.connect("notify::active", self._on_settings_vcam_changed)
        self._settings_page.connect("help-tooltips-changed", self._on_help_tooltips_changed)
        self._settings_page.connect("capture-timer-changed", self._on_capture_timer_changed)
        self._settings_page.connect("recording-config-changed", self._on_recording_config_changed)
        self._settings_page.connect("prefer-v4l2-changed", self._on_prefer_v4l2_changed)
        self._settings_page.connect("resource-monitor-changed", self._on_resource_monitor_changed)
        self.connect("close-request", self._on_close)
        self.connect("map", self._on_window_mapped)

    # -- signal handlers -----------------------------------------------------

    # Cache controls per camera to avoid PTP re-access (instance-level)
    # Track cameras that already showed the vcam creation dialog (instance-level)

    def _pick_preferred_format(self, camera: CameraInfo):
        """Return a VideoFormat matching user resolution/FPS preferences, or None."""
        from core.camera_backend import VideoFormat

        res_pref = self._settings.get(
            "preferred-resolution"
        )  # "" / "480" / "720" / "1080" / "2160"
        fps_pref = self._settings.get("fps-limit")  # 0=auto / 15 / 24 / 30 / 60

        if not res_pref and not fps_pref:
            return None  # auto

        if not camera.formats:
            return None

        _RES_MAP = {"480": 480, "720": 720, "1080": 1080, "2160": 2160}
        target_h = _RES_MAP.get(res_pref, 0)

        candidates = camera.formats
        if target_h:
            # Find formats matching the target height
            exact = [f for f in candidates if f.height == target_h]
            if exact:
                candidates = exact
            else:
                # Pick closest height
                candidates = sorted(candidates, key=lambda f: abs(f.height - target_h))
                closest_h = candidates[0].height
                candidates = [f for f in candidates if f.height == closest_h]

        if fps_pref and fps_pref > 0:
            # Filter formats that support the desired FPS (or closest)
            best = None
            for fmt in candidates:
                if fps_pref in fmt.fps or any(f >= fps_pref for f in fmt.fps):
                    best = fmt
                    break
            if best is None and candidates:
                best = candidates[0]
            if best is not None:
                # Create a copy with fps capped to the preference
                capped_fps = [f for f in best.fps if f <= fps_pref]
                if not capped_fps:
                    capped_fps = best.fps
                return VideoFormat(
                    width=best.width,
                    height=best.height,
                    fps=capped_fps,
                    pixel_format=best.pixel_format,
                    description=best.description,
                )
            return None

        return candidates[0] if candidates else None

    def _on_camera_selected(
        self, _selector: CameraSelector, camera: CameraInfo
    ) -> None:
        if self._closing:
            return
        if self._video_recorder.is_recording or self._video_recorder.is_finalizing or self._capture_pending:
            self._show_notification(_("Finish the current capture before changing cameras."), "warning")
            if self._active_camera:
                cameras = self._camera_manager.cameras
                for index, candidate in enumerate(cameras):
                    if candidate.id == self._active_camera.id:
                        self._camera_selector.set_selected_silent(index)
                        break
            return
        self._preview.cancel_countdown()
        self._selection_generation += 1
        generation = self._selection_generation
        log.info("Camera selection started")
        # Skip if same camera is already active — period.
        if self._active_camera and self._active_camera.id == camera.id:
            log.info("Camera %s already active, skipping", camera.name)
            return

        # Block dropdown signals during the entire camera setup.
        # GTK DropDown changes its internal selected state when model items'
        # GObject properties change (set_active_camera) or when child widgets
        # are reconfigured. This would trigger an infinite selection cycle.
        self._camera_selector.block_signals()

        old_active = self._active_camera

        self._active_camera = camera
        self._camera_selector.set_active_camera(camera.id)
        self._settings.set("last-camera-id", camera.id)
        self.set_title(f"{APP_NAME} — {camera.name}")
        
        # If we are switching away from a camera, ensure it resumes streaming in the background
        if old_active and old_active.id != camera.id:
            # We delay the bg vcam creation slightly to ensure the main pipeline has
            # fully released the device, avoiding Resource Busy errors.
            GLib.timeout_add(500, self._stream_engine.ensure_bg_vcam, old_active)

        # Check if backend needs streaming setup (e.g. gphoto2)
        backend = self._camera_manager.get_backend(camera.backend)
        needs_setup = (
            backend
            and hasattr(backend, "needs_streaming_setup")
            and backend.needs_streaming_setup()
        )

        log.debug(
            f"Camera selected: {camera.name}, backend={camera.backend}, needs_setup={needs_setup}"
        )

        if needs_setup:
            # Prevent concurrent streaming attempts — ignore if already in progress
            if self._streaming_lock.locked():
                log.debug("Streaming already in progress, ignoring selection")
                self._camera_selector.unblock_signals()
                return

            # Stop hotplug polling to prevent gphoto2 --auto-detect racing with streaming
            self._camera_manager.stop_hotplug()

            # Check if this camera already has a streaming session alive
            already_streaming = hasattr(
                backend, "is_camera_streaming"
            ) and backend.is_camera_streaming(camera)
            cached_controls = self._controls_cache.get(camera.id)

            log.debug(
                f"already_streaming={already_streaming}, cached_controls={cached_controls is not None}, camera.id={camera.id}"
            )
            if hasattr(backend, "_active_streams"):
                log.debug(f"_active_streams={dict(backend._active_streams)}")

            if already_streaming and cached_controls is not None:
                # Hot-swap: camera already streaming, just switch the GStreamer pipeline
                log.debug(f"Hot-swap to {camera.name} (already streaming)")
                self._stream_engine.stop(stop_backend=False, keep_vcam=True)
                self._controls_page.set_camera_with_controls(camera, cached_controls)
                self._stream_engine.play(camera, streaming_ready=True)
                self._show_vcam_dialog(camera)
                # Resume hotplug monitoring after hot-swap
                if self._settings.get("hotplug_enabled"):
                    self._camera_manager.start_hotplug()
                self._camera_selector.unblock_signals()
                return

            # Stop only the GStreamer pipeline, keep other cameras' backend alive
            self._stream_engine.stop(stop_backend=False, keep_vcam=True)

            self._preview.show_status(
                _("Please wait…"),
                _("Starting camera stream…"),
                loading=True,
            )

            def do_controls_then_stream() -> tuple[bool, list]:
                """Fetch controls BEFORE streaming (gphoto2 locks USB)."""
                if not self._streaming_lock.acquire(blocking=False):
                    log.debug("Lock already held, aborting")
                    return False, []
                try:
                    # Pre-allocate v4l2loopback device (subprocess calls)
                    if camera.backend == BackendType.GPHOTO2:
                        vcam_dev = VirtualCamera.ensure_ready(
                            card_label=camera.name,
                            camera_id=camera.id,
                        )
                        if vcam_dev:
                            camera.extra["vcam_device"] = vcam_dev
                            log.info("Pre-allocated vcam %s for gphoto2 camera %s", vcam_dev, camera.name)

                    controls = cached_controls
                    if controls is None:
                        log.debug("Fetching gPhoto2 controls before streaming...")
                        controls = self._camera_manager.get_controls(camera)
                        log.debug(f"Got {len(controls)} controls")

                    if already_streaming:
                        log.debug("Camera already streaming, skipping start")
                        return True, controls

                    log.debug("Starting streaming...")
                    success = backend.start_streaming(camera)
                    log.debug(f"Streaming result: {success}")
                    if success:
                        GLib.idle_add(
                            lambda: (
                                self._show_notification(
                                    _("Camera streaming started!"), "success", 3000
                                )
                                or False
                            )
                        )
                    return success, controls
                finally:
                    self._streaming_lock.release()

            def on_done(result: tuple[bool, list]) -> None:
                if self._closing or generation != self._selection_generation:
                    return
                success, controls = result
                log.debug(f"on_done: success={success}, controls={len(controls)}")
                self._dismiss_notification()
                if success:
                    self._controls_cache[camera.id] = controls
                    self._controls_page.set_camera_with_controls(camera, controls)
                    self._stream_engine.play(camera, streaming_ready=True)
                    self._show_vcam_dialog(camera)
                elif camera.extra.get("capture_unsupported"):
                    self._preview.show_status(
                        _("Capture not supported"),
                        _(
                            "This camera does not support live streaming via USB. "
                            "Its PTP driver only allows file transfer. "
                            "Use an HDMI capture card to stream from this camera."
                        ),
                        icon="dialog-warning-symbolic",
                    )
                    self._show_notification(
                        _("Camera does not support USB streaming."), "error", 8000
                    )
                elif camera.extra.get("ptp_streaming_error"):
                    self._preview.show_status(
                        _("Streaming failed"),
                        _(
                            "The camera returned PTP errors during video capture. "
                            "It may lack PC Remote mode or its USB connection mode "
                            "needs to be changed. Check the camera menu for USB "
                            "settings and select 'PC Remote' if available.\n\n"
                            "Alternatively, use an HDMI capture card."
                        ),
                        icon="dialog-warning-symbolic",
                    )
                    self._show_notification(
                        _("Camera PTP streaming failed."), "error", 8000
                    )
                else:
                    self._show_notification(
                        _("Failed to start camera streaming."), "error"
                    )
                    self._preview._show_retry()
                # Resume hotplug monitoring after gphoto2 setup completes
                if self._settings.get("hotplug_enabled"):
                    self._camera_manager.start_hotplug()
                # Unblock dropdown signals after async setup completes
                self._camera_selector.unblock_signals()

            def on_setup_error(exc):
                if not self._closing and generation == self._selection_generation:
                    self._camera_selector.unblock_signals()
                    self._preview._on_error(self._stream_engine, _("Failed to start camera streaming."))
                    if self._settings.get("hotplug_enabled"):
                        self._camera_manager.start_hotplug()
            run_async(do_controls_then_stream, on_success=on_done, on_error=on_setup_error)
        else:
            # V4L2, libcamera, PipeWire: load controls async + start stream
            self._preview.show_status(
                _("Please wait…"),
                _("Starting camera stream…"),
                loading=True,
            )
            self._stream_engine.stop(stop_backend=False, keep_vcam=True)

            # Start the V4L2 camera immediately
            self._controls_page.set_camera(camera)
            self._settings_page.update_camera_formats(camera)
            preferred_fmt = self._pick_preferred_format(camera)
            self._stream_engine.play(camera, fmt=preferred_fmt)



            # Show virtual camera dialog
            self._show_vcam_dialog(camera)

            # Unblock dropdown signals after synchronous setup
            self._camera_selector.unblock_signals()

    def _show_vcam_dialog(self, camera: CameraInfo) -> None:
        """Show a notice informing the user about the virtual camera created (once per device)."""
        if camera.id in self._vcam_dialog_shown:
            return
        vcam_device = VirtualCamera.get_device_for_camera(camera.id)
        if not vcam_device:
            return
        self._vcam_dialog_shown.add(camera.id)
        
        # Show a simple notice instead of a blocking dialog
        msg = _("Virtual Camera: {vcam_device} Created!").format(vcam_device=vcam_device)
        self._show_notification(msg, "info", 4000)

    def _on_retry(self, _preview: PreviewArea) -> None:
        """Re-attempt camera connection when user clicks Try Again."""
        if self._active_camera:
            cam = self._active_camera
            self._active_camera = None  # Clear so same-camera guard doesn't skip
            self._stream_engine.stop()
            self._on_camera_selected(self._camera_selector, cam)

    def _on_virtual_camera_toggled(self, _page, _enabled: bool) -> None:
        """Restart stream to add/remove virtual camera loopback output."""
        self._settings.set("virtual-camera-enabled", _enabled)
        self._update_vcam_badge()
        # Stop all background vcam pipelines first
        self._stream_engine.stop_all_bg_vcams()
        if self._active_camera:
            cam = self._active_camera
            self._active_camera = None  # Clear so same-camera guard doesn't skip
            self._stream_engine.stop(stop_backend=False)
            # All streams stopped — safe to clean up stale dynamic devices
            VirtualCamera.cleanup_dynamic_devices()
            self._on_camera_selected(self._camera_selector, cam)
        else:
            VirtualCamera.cleanup_dynamic_devices()
        # Recreate background vcams for all cameras if enabled
        if _enabled:
            for cam in self._camera_manager.cameras:
                self._stream_engine.ensure_bg_vcam(cam)

    def _on_virtual_camera_device_toggled(self, _page, camera_id: str, active: bool) -> None:
        """Handle per-device virtual camera toggle."""
        self._update_vcam_badge()
        if active:
            log.info("Virtual camera enabled for %s", camera_id)
            if self._active_camera and self._active_camera.id == camera_id:
                # Restart active stream to hook up the virtual sink
                cam = self._active_camera
                self._active_camera = None
                self._stream_engine.stop(stop_backend=False)
                self._on_camera_selected(self._camera_selector, cam)
            else:
                cam = next((c for c in self._camera_manager.cameras if c.id == camera_id), None)
                if cam:
                    self._stream_engine.ensure_bg_vcam(cam)
        else:
            log.info("Virtual camera disabled for %s", camera_id)
            if self._active_camera and self._active_camera.id == camera_id:
                cam = self._active_camera
                self._active_camera = None
                self._stream_engine.stop(stop_backend=False)
                self._on_camera_selected(self._camera_selector, cam)
            else:
                self._stream_engine._stop_bg_vcam(camera_id)
            # Make sure the device is fully released
            from core.virtual_camera import VirtualCamera
            VirtualCamera.release_device(camera_id)

    def _on_show_fps_changed(self, _page, show: bool) -> None:
        self._preview.set_show_fps(show)

    def _on_mirror_changed(self, _page, mirror: bool) -> None:
        self._stream_engine.mirror = mirror
        self._preview.set_mirror(mirror)
        self._mirror_btn.handler_block(self._mirror_btn_handler_id)
        self._mirror_btn.set_active(mirror)
        self._mirror_btn.handler_unblock(self._mirror_btn_handler_id)

    def _on_prefer_v4l2_changed(self, _page, prefer: bool) -> None:
        self._stream_engine.prefer_v4l2 = prefer

    def _on_resolution_changed(self, _page, value: str) -> None:
        if self._video_recorder.is_recording or self._video_recorder.is_finalizing:
            self._show_notification(_("Camera preferences will apply after recording stops."))
            return
        if self._active_camera:
            log.info("Resolution changed to '%s', restarting stream", value)
            preferred_fmt = self._pick_preferred_format(self._active_camera)
            self._stream_engine.play(self._active_camera, fmt=preferred_fmt)

    def _on_fps_limit_changed(self, _page, value: int) -> None:
        if self._video_recorder.is_recording or self._video_recorder.is_finalizing:
            self._show_notification(_("Camera preferences will apply after recording stops."))
            return
        if self._active_camera:
            preferred_fmt = self._pick_preferred_format(self._active_camera)
            self._stream_engine.play(self._active_camera, fmt=preferred_fmt)

    def _on_grid_overlay_changed(self, _page, visible):
        self._preview.set_grid_visible(visible)
        if self._grid_btn.get_active() != visible:
            self._grid_btn.set_active(visible)

    def _on_overlay_opacity_changed(self, _page, value: int) -> None:
        self._apply_overlay_opacity(value)

    def _on_controls_opacity_changed(self, _page, value: int) -> None:
        self._apply_controls_opacity(value)

    def _on_window_opacity_changed(self, _page, value: int) -> None:
        self._apply_window_opacity(value)

    def _apply_window_opacity(self, percent: int) -> None:
        """Adjust the window background opacity (0 = fully transparent, 100 = opaque)."""
        alpha = percent / 100.0
        css = (
            f"window.bigcam {{"
            f"  background-color: rgba(0,0,0,{alpha:.2f});"
            f"}}\n"
            f".preview-area {{"
            f"  background-color: rgba(0,0,0,{alpha:.2f});"
            f"}}\n"
            f".preview-picture {{"
            f"  background-color: rgba(0,0,0,{alpha:.2f});"
            f"}}\n"
            f".video-bg {{"
            f"  background-color: rgba(0,0,0,{alpha:.2f});"
            f"}}"
        )
        if not hasattr(self, "_window_opacity_provider"):
            self._window_opacity_provider = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                self._window_opacity_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 3,
            )
        self._window_opacity_provider.load_from_string(css)

    def _apply_overlay_opacity(self, percent: int) -> None:
        """Regenerate gradient CSS for the top/bottom OSD bars."""
        alpha = percent / 100.0
        mid = alpha * 0.6  # mid-point is 60% of main opacity
        css = (
            f".osd-bar.top-bar {{"
            f"  background: linear-gradient(to bottom,"
            f"    rgba(0,0,0,{alpha:.2f}) 0%,"
            f"    rgba(0,0,0,{mid:.2f}) 70%,"
            f"    transparent 100%);"
            f"}}\n"
            f".osd-bar.bottom-bar {{"
            f"  background: linear-gradient(to top,"
            f"    rgba(0,0,0,{alpha:.2f}) 0%,"
            f"    rgba(0,0,0,{mid:.2f}) 70%,"
            f"    transparent 100%);"
            f"}}"
        )
        if not hasattr(self, "_overlay_css_provider"):
            self._overlay_css_provider = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                self._overlay_css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1,
            )
        self._overlay_css_provider.load_from_string(css)

    def _apply_controls_opacity(self, percent: int) -> None:
        """Adjust opacity of buttons/icons overlaid on the preview."""
        alpha = percent / 100.0
        css = (
            f".osd-bar.top-bar button {{"
            f"  opacity: {alpha:.2f};"
            f"}}\n"
            f".osd-bar.top-bar button:hover {{"
            f"  opacity: 1.0;"
            f"}}\n"
            f".bottom-circle-btn {{"
            f"  opacity: {alpha:.2f};"
            f"}}\n"
            f".bottom-circle-btn:hover {{"
            f"  opacity: 1.0;"
            f"}}\n"
            f".mode-switcher {{"
            f"  opacity: {alpha:.2f};"
            f"}}\n"
            f".mode-switcher:hover {{"
            f"  opacity: 1.0;"
            f"}}\n"
            f".capture-button {{"
            f"  opacity: {alpha:.2f};"
            f"}}\n"
            f".capture-button:hover {{"
            f"  opacity: 1.0;"
            f"}}\n"
            f".audio-overlay {{"
            f"  opacity: {alpha:.2f};"
            f"}}\n"
            f".audio-overlay:hover {{"
            f"  opacity: 1.0;"
            f"}}\n"
            f"button.zoom-btn {{"
            f"  opacity: {alpha:.2f};"
            f"}}\n"
            f"button.zoom-btn:hover {{"
            f"  opacity: 1.0;"
            f"}}\n"
            f"button.last-photo-btn {{"
            f"  opacity: {alpha:.2f};"
            f"}}\n"
            f"button.last-photo-btn:hover {{"
            f"  opacity: 1.0;"
            f"}}\n"
            f".record-button {{"
            f"  opacity: {alpha:.2f};"
            f"}}\n"
            f".record-button:hover {{"
            f"  opacity: 1.0;"
            f"}}\n"
            f".phone-webcam-button {{"
            f"  opacity: {alpha:.2f};"
            f"}}\n"
            f".phone-webcam-button:hover {{"
            f"  opacity: 1.0;"
            f"}}"
        )
        if not hasattr(self, "_controls_css_provider"):
            self._controls_css_provider = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                self._controls_css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 2,
            )
        self._controls_css_provider.load_from_string(css)

    # -- Tools signals -------------------------------------------------------

    def _on_qr_detected(self, _page: Any, text: str) -> None:
        self._show_notification(_("QR Code detected!"), "info", 2000)

    # -- Capture -------------------------------------------------------------

    def _on_capture(self, _preview: PreviewArea) -> None:
        if self._closing or self._capture_pending:
            return
        if not self._active_camera:
            self._show_notification(_("No camera selected."), "warning")
            return

        from constants import BackendType

        # gPhoto2: ask capture mode BEFORE starting timer
        if self._active_camera.backend == BackendType.GPHOTO2:
            dialog = Adw.AlertDialog.new(
                _("Choose capture mode"),
                _(
                    "You can take a screenshot from the current preview "
                    "or capture a full-resolution photo directly from the camera."
                ),
            )
            dialog.add_response("webcam", _("Preview screenshot"))
            dialog.add_response("native", _("Camera photo (full resolution)"))
            dialog.set_response_appearance("native", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("native")
            dialog.add_response("cancel", _("Cancel"))
            dialog.set_close_response("cancel")
            dialog.connect("response", self._on_capture_mode_response)
            self._immersion.present_dialog(dialog, self)
            return

        timer = self._settings.get("capture-timer")
        if timer and timer > 0:
            self._preview.start_countdown(timer, self._do_webcam_capture)
            return

        self._do_webcam_capture()

    def _on_capture_mode_response(
        self, _dialog: Adw.AlertDialog, response: str
    ) -> None:
        if response not in {"native", "webcam"} or self._closing:
            return
        capture_fn = self._do_native_capture if response == "native" else self._do_webcam_capture
        timer = self._settings.get("capture-timer")
        if timer and timer > 0:
            self._preview.start_countdown(timer, capture_fn)
            return
        capture_fn()

    def _show_movie_mode_dialog(self, camera: CameraInfo) -> None:
        """Show dialog when camera is in Movie mode and can't take stills."""
        dialog = Adw.AlertDialog.new(
            _("Camera in video mode"),
            _(
                "Your camera is set to Video/Movie mode. "
                "Some cameras cannot take photos in this mode.\n\n"
                "Switch the mode dial on your camera to a photo mode "
                "(P, Av, Tv, M or Auto) and try again, or capture "
                "a frame from the current preview."
            ),
        )
        dialog.add_response("frame", _("Capture preview frame"))
        dialog.add_response("retry", _("Try again"))
        dialog.set_response_appearance("retry", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("retry")
        dialog.add_response("cancel", _("Cancel"))
        dialog.set_close_response("cancel")

        def _on_response(_dlg: Adw.AlertDialog, resp: str) -> None:
            # Always resume streaming first
            self._active_camera = None
            self._on_camera_selected(self._camera_selector, camera)

            if resp == "frame":
                # Wait for stream to stabilise, then grab a frame
                GLib.timeout_add(2000, lambda: self._do_webcam_capture() or False)
            # "retry" just resumes the preview — user clicks capture again

        dialog.connect("response", _on_response)
        self._immersion.present_dialog(dialog, self)

    def _do_webcam_capture(self):
        if self._closing or self._capture_pending or not self._active_camera:
            return
        self._capture_pending = True
        generation = self._selection_generation
        self._show_notification(_("Capturing photo…"), timeout_ms=0)
        def capture():
            path = reserve_media_path(xdg.photos_dir(), ".png")
            if not self._stream_engine.capture_snapshot(path):
                os.unlink(path)
                raise RuntimeError("No fresh frame could be saved")
            return path
        def saved(path):
            self._capture_pending = False
            if self._closing or generation != self._selection_generation:
                return
            self._trigger_flash()
            self._show_notification(_("Photo saved!"), "success")
            self._gallery.refresh()
            self._update_last_media_thumbnail(path)
        def failed(exc):
            self._capture_pending = False
            if not self._closing:
                self._show_notification(_("Failed to capture photo."), "error", 0)
        run_async(capture, on_success=saved, on_error=failed)

    def _do_native_capture(self):
        camera = self._active_camera
        if not camera or self._closing or self._capture_pending:
            return
        self._capture_pending = True
        generation = self._selection_generation
        self._camera_manager.stop_hotplug()
        self._stream_engine.stop(stop_backend=False)
        self._preview.show_status(_("Please wait…"), _("Switching to photography mode."), loading=True)
        def capture():
            path = reserve_media_path(xdg.photos_dir(), ".jpg")
            if not self._camera_manager.capture_photo(camera, path):
                if os.path.getsize(path) == 0:
                    os.unlink(path)
                raise RuntimeError("Native capture failed")
            # Preserve the original JPEG and its EXIF/ICC metadata byte-for-byte.
            return path
        def resume(path=None, error=None):
            self._capture_pending = False
            if self._closing or generation != self._selection_generation:
                return
            if path:
                self._trigger_flash()
                self._gallery.refresh()
                self._update_last_media_thumbnail(path)
                self._show_notification(_("Photo saved!"), "success")
            else:
                self._show_notification(_("Failed to capture photo. Check the camera mode and connection."), "error", 0)
            self._active_camera = None
            self._on_camera_selected(self._camera_selector, camera)
        run_async(capture, on_success=lambda path: resume(path), on_error=lambda exc: resume(error=exc))

    def _on_refresh(self, *_args):
        if self._closing or self._video_recorder.is_recording or self._video_recorder.is_finalizing or self._capture_pending:
            return
        self._selection_generation += 1
        self._preview.cancel_countdown()
        self._stream_engine.stop()
        self._active_camera = None
        self._camera_manager.detect_cameras_async(force_emit=True)

    def _on_add_ip(self, *_args) -> None:
        dialog = IPCameraDialog()
        dialog.connect("camera-added", self._on_ip_camera_added)
        self._immersion.present_dialog(dialog, self)

    def _draw_phone_dot(self, area: Gtk.DrawingArea, cr, w: int, h: int) -> None:
        """Draw a colored status dot on the phone button."""
        r, g, b = self._phone_status_color
        cr.set_source_rgb(r, g, b)
        cr.arc(w / 2, h / 2, min(w, h) / 2, 0, 2 * 3.14159265)
        cr.fill()

    def _on_vcam_limit_reached(self, _bus, max_devices: int) -> None:
        GLib.idle_add(self._show_vcam_limit_dialog, max_devices)

    def _show_vcam_limit_dialog(self, max_devices: int) -> None:
        dialog = Adw.AlertDialog.new(
            _("Virtual Camera Limit Reached"),
            _("The maximum limit of {} virtual cameras has been reached. Please disconnect some cameras or disable background virtual cameras in settings to connect a new one.").format(max_devices)
        )
        dialog.add_response("ok", _("OK"))
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)

    def _on_mobile_status_changed(self, _bus, backend: str, status: str) -> None:
        """Update the phone button dot color dynamically from EventBus."""
        colors = {
            "listening": (1.0, 0.76, 0.03),
            "starting": (1.0, 0.76, 0.03),
            "connected": (0.16, 0.65, 0.27),
            "stopped": (0.6, 0.6, 0.6),
            "disconnected": (0.6, 0.6, 0.6),
            "error": (0.85, 0.2, 0.2),
        }
        
        lower_status = status.lower()
        color = (1.0, 0.76, 0.03) # yellow default

        for key, c in colors.items():
            if key in lower_status:
                color = c
                break

        visible = "stopped" not in lower_status and "disconnected" not in lower_status

        self._phone_status_color = color
        self._phone_dot.set_visible(visible)
        self._phone_dot.queue_draw()

        if "connected" in lower_status and "disconnected" not in lower_status:
            self._phone_btn.add_css_class("phone-connected")
        else:
            self._phone_btn.remove_css_class("phone-connected")

    def _on_phone_camera(self, *_args) -> None:
        self._mobile_device_ctrl.show_dialog(self)

    def _switch_to_phone_camera(self, camera_id: str) -> None:
        """Switch to a phone camera source (called from toast button)."""
        self._stream_engine._stop_bg_vcam(camera_id)
        self._stream_engine._stop_bg_phone_vcam()
        self._select_camera_by_id(camera_id)
        if self._settings.get("hotplug_enabled"):
            self._camera_manager.start_hotplug()

    def _on_eventbus_camera_changed(self, _bus, cam_info) -> None:
        """Handle new mobile cameras from EventBus."""
        if cam_info is None:
            # A camera disconnected. If it was active, reset.
            was_active = self._active_camera and self._active_camera.id.startswith("phone:")
            if was_active:
                self._stream_engine.stop()
                self._active_camera = None
                self._block_camera_select = False
                if self._settings.get("hotplug_enabled"):
                    self._camera_manager.start_hotplug()
                self._camera_manager.detect_cameras_async(force_emit=True)
                if not self._camera_manager.cameras:
                    self._camera_selector.set_active_camera(None)
                    self._controls_page.set_camera(None)
                    self._preview.show_status(
                        _("No camera"),
                        _("Connect a camera or select one from the list above."),
                    )
                    self.set_title(APP_NAME)
        else:
            # A camera connected
            self._camera_manager.stop_hotplug()
            self._stream_engine._stop_bg_vcam(cam_info.id)
            
            toast = Adw.Toast.new(f"📱  {cam_info.name}")
            toast.set_timeout(6)
            toast.set_button_label(_("Show"))
            toast.connect(
                "button-clicked",
                lambda _t: self._switch_to_phone_camera(cam_info.id),
            )
            self._toast_overlay.add_toast(toast)


    def _on_ip_camera_added(self, _dialog: IPCameraDialog, name: str, url: str) -> None:
        ip_list = self._settings.get("ip_cameras")
        if not isinstance(ip_list, list):
            ip_list = list(ip_list) if ip_list else []
        ip_list.append({"name": name, "url": url})
        self._settings.set("ip_cameras", ip_list)
        self._camera_manager.add_ip_cameras(ip_list)

    def _on_about(self, *_args) -> None:
        from ui.about_dialog import create_about_dialog
        dialog = create_about_dialog()
        self._immersion.present_dialog(dialog, self)

    def _on_camera_error(self, _manager: CameraManager, message: str) -> None:
        self._show_notification(message, "error", 5000)

    def _on_device_busy(
        self,
        _engine: StreamEngine,
        device_path: str,
        blocking_apps: list[str],
    ) -> None:
        """Show an informative dialog when another app holds the camera."""
        # Save the camera reference before clearing it
        busy_camera = self._active_camera
        camera_name = busy_camera.name if busy_camera else ""

        if busy_camera and busy_camera.backend in (BackendType.PHONE, BackendType.SCRCPY, BackendType.AIRPLAY):
            blocking_apps = [name for name in blocking_apps if name not in {"scrcpy", "uxplay"}]

        # Clear active camera so 'Refresh cameras' can re-select it
        self._active_camera = None

        cam_label = camera_name or device_path
        if blocking_apps:
            apps_str = ", ".join(blocking_apps)
            body = _(
                "The camera \"%(camera)s\" is being used by: %(apps)s.\n\n"
                "Close the other application or force-close it to free the camera."
            ) % {"camera": cam_label, "apps": apps_str}
        else:
            body = _(
                "The camera \"%s\" is being used by another application.\n\n"
                "Close the other application to free the camera."
            ) % cam_label

        dialog = Adw.AlertDialog.new(_("Camera in use"), body)

        dialog.add_response("retry", _("Try again"))
        dialog.set_response_appearance(
            "retry", Adw.ResponseAppearance.SUGGESTED
        )

        if blocking_apps:
            first_app = blocking_apps[0]
            dialog.add_response(
                "force-close",
                _("Force close %s") % first_app,
            )
            dialog.set_response_appearance(
                "force-close", Adw.ResponseAppearance.DESTRUCTIVE
            )

        dialog.add_response("close", _("Close"))
        dialog.set_default_response("retry")
        dialog.set_close_response("close")

        dialog.connect(
            "response",
            self._on_device_busy_response,
            device_path,
            blocking_apps,
            busy_camera,
        )
        self._immersion.present_dialog(dialog, self)

    def _on_device_busy_response(
        self,
        _dialog: Adw.AlertDialog,
        response: str,
        device_path: str,
        blocking_apps: list[str],
        camera: "CameraInfo | None",
    ) -> None:
        if response == "force-close":
            self._force_close_device_users(device_path, blocking_apps, camera)
        elif response == "retry":
            self._retry_camera(camera)

    def _retry_camera(self, camera: "CameraInfo | None") -> None:
        """Force-retry the given camera, bypassing the 'already active' guard."""
        if not camera:
            return
        # Ensure active is clear so _on_camera_selected doesn't skip
        self._active_camera = None
        self._on_camera_selected(self._camera_selector, camera)

    def _force_close_device_users(
        self,
        device_path: str,
        blocking_apps: list[str],
        camera: "CameraInfo | None" = None,
    ) -> None:
        """Terminate processes using the device, then retry the camera."""
        import signal as sig

        def _kill_users() -> None:
            own_pid = str(os.getpid())
            own_uid = os.getuid()
            try:
                result = subprocess.run(
                    ["fuser", device_path],
                    capture_output=True,
                    text=True,
                    timeout=3,
                )
                pids = result.stdout.strip().split()
                for pid in pids:
                    pid = pid.strip().rstrip("m")
                    if pid.isdigit() and pid != own_pid:
                        # Only signal processes owned by the current user
                        try:
                            stat_path = f"/proc/{pid}/status"
                            with open(stat_path) as f:
                                for line in f:
                                    if line.startswith("Uid:"):
                                        proc_uid = int(line.split()[1])
                                        break
                                else:
                                    continue
                            if proc_uid != own_uid:
                                log.warning("Skipping PID %s (UID %d != %d)", pid, proc_uid, own_uid)
                                continue
                        except (OSError, ValueError):
                            continue
                        try:
                            os.kill(int(pid), sig.SIGTERM)
                        except ProcessLookupError:
                            pass
            except Exception:
                log.warning("Failed to kill processes on %s", device_path, exc_info=True)

        def _on_done(_result: None = None) -> None:
            GLib.timeout_add(2000, lambda: self._retry_camera(camera) or False)

        run_async(_kill_users, on_success=_on_done)

    def _retry_after_force_close(self) -> bool:
        if self._active_camera:
            cam = self._active_camera
            self._active_camera = None
            self._on_camera_selected(self._camera_selector, cam)
        return False

    def _activate_virtual_camera_from_dialog(self) -> None:
        """Enable virtual camera and restart the pipeline."""
        VirtualCamera.set_enabled(True)
        self._settings_page.set_vc_toggle_active(True)
        self._settings.set("virtual-camera-enabled", True)
        self._show_notification(
            _("Virtual Camera enabled. Other applications can use /dev/video10."),
            "success",
            5000,
        )
        if self._active_camera:
            GLib.timeout_add(500, self._retry_after_force_close)

    # -- recording -----------------------------------------------------------

    def _on_capture_action(self, *_args) -> None:
        if self._is_editing_text():
            return
        if self._current_mode == "video":
            self._on_record_toggle()
            return
        self._on_capture(self._preview)

    def _on_record_toggle(self, *_args):
        if self._closing:
            return
        recorder = self._video_recorder
        if recorder.is_finalizing:
            self._show_notification(_("Finalizing video…"), timeout_ms=0)
            return
        if recorder.is_recording:
            recorder.stop()
            return
        if not self._active_camera or not self._stream_engine.is_playing():
            self._show_notification(_("No active camera stream."), "warning")
            return
        try:
            sources = self._audio_monitor.all_source_names
            path = recorder.start(
                self._active_camera,
                audio_sources=sources,
                external_audio=self._audio_monitor.external_recording_sources,
                active_audio_sources=[name for name in self._audio_monitor.active_source_names if name in sources],
                source_volumes={name: self._audio_monitor.get_source_volume(name) for name in sources},
                muted=self._audio_monitor.muted,
                fps=self._stream_engine.fps,
            )
        except (OSError, ValueError):
            log.exception("Cannot reserve recording output")
            self._show_notification(_("Failed to start recording."), "error", 0)
            return
        if path:
            if not self._recording_hold:
                self.get_application().hold()
                self._recording_hold = True
            self._immersion.inhibit()
            self._show_notification(_("Starting recording…"), timeout_ms=0)


    def _on_recording_state(self, recorder, state):
        if self._closing:
            return
        recording = state in {"starting", "recording"}
        finalizing = state == "finalizing"
        self._preview.set_recording_state(recording)
        self._mode_photo_btn.set_sensitive(not (recording or finalizing))
        self._mode_video_btn.set_sensitive(not (recording or finalizing))
        self._bottom_capture_btn.set_sensitive(not finalizing)
        if state == "recording":
            self._start_rec_timer()
            self._show_notification(_("Recording…"), timeout_ms=0)
        elif finalizing:
            self._stop_rec_timer()
            self._show_notification(_("Finalizing video…"), timeout_ms=0)
        if self._current_mode == "video":
            self._bottom_capture_btn.set_icon_name("media-playback-stop-symbolic" if recording else "media-record-symbolic")
            self._update_tooltip(self._bottom_capture_btn, _("Stop recording") if recording else _("Start recording"))
            if recording:
                self._bottom_capture_btn.add_css_class("recording")
            else:
                self._bottom_capture_btn.remove_css_class("recording")


    def _on_recording_finalized(self, recorder, path, success, error):
        if self._recording_hold:
            self._recording_hold = False
            self.get_application().release()
        if self._closing:
            return
        self._stop_rec_timer()
        self._immersion.uninhibit()
        if success:
            self._show_notification(_("Video saved: %s") % os.path.basename(path), "success")
            self._video_gallery.refresh()
            self._update_last_media_thumbnail(path)
        else:
            self._show_notification(_("Recording failed. Any partial file has been preserved: %s") % os.path.basename(path), "error", 0)
            log.error("Recording finalization failed: %s", error)

    def _on_audio_source_toggled(
        self, _monitor: AudioMonitor, source_name: str, active: bool
    ) -> None:
        """Forward audio source toggle to the video recorder during recording."""
        if self._video_recorder.is_recording:
            self._video_recorder.set_source_active(source_name, active)

    def _on_audio_source_volume_changed(
        self, _monitor: AudioMonitor, source_name: str, volume: float
    ) -> None:
        """Forward per-source volume change to the video recorder during recording."""
        if self._video_recorder.is_recording:
            self._video_recorder.set_source_volume(source_name, volume)

    def _on_audio_mute_changed(self, _monitor: AudioMonitor, muted: bool) -> None:
        """Forward global mute change to the video recorder during recording."""
        if self._video_recorder.is_recording:
            self._video_recorder.set_muted(muted)

    # -- profiles ------------------------------------------------------------

    def _on_save_profile(self, *_args) -> None:
        if not self._active_camera:
            return
        camera = self._active_camera

        def _save() -> list:
            controls = self._camera_manager.get_controls(camera)
            if controls:
                camera_profiles.save_profile(camera, "default", controls)
            return controls

        def _on_saved(controls: list) -> None:
            if controls:
                self._show_notification(_("Profile saved."), "success")

        run_async(_save, on_success=_on_saved)

    def _on_load_profile(self, *_args) -> None:
        if not self._active_camera:
            return
        camera = self._active_camera
        profiles = camera_profiles.list_profiles(camera)
        if not profiles:
            self._show_notification(_("No profiles found."), "info")
            return
        name = profiles[0]
        values = camera_profiles.load_profile(camera, name)

        def _apply() -> None:
            for ctrl_id, value in values.items():
                self._camera_manager.set_control(camera, ctrl_id, value)

        def _on_applied(_result: None = None) -> None:
            self._controls_page.set_camera(camera)
            self._show_notification(
                _("Profile loaded: %s") % name, "success"
            )

        run_async(_apply, on_success=_on_applied)

    # -- auto-start preview --------------------------------------------------

    def _on_cameras_changed_auto_start(self, _manager: CameraManager) -> None:
        """Auto-start preview with the last used camera, or the first available."""
        # Skip auto-start while phone/scrcpy camera setup is in progress
        if getattr(self, "_block_camera_select", False):
            return

        current_ids = {c.id for c in self._camera_manager.cameras}
        self._update_vcam_badge()

        # Re-detect audio sources when USB devices change
        self._audio_monitor.detect_all()

        # Stop background vcams for cameras that were disconnected
        removed_ids = self._known_camera_ids - current_ids
        for cam_id in removed_ids:
            self._stream_engine._stop_bg_vcam(cam_id)
            VirtualCamera.release_device(cam_id)
            log.info("Released vcam for disconnected camera %s", cam_id)

        # If the active camera was removed and no other cameras exist, stop it
        if self._active_camera and self._active_camera.id in removed_ids:
            if not self._camera_manager.cameras:
                self._stream_engine.stop()
                self._active_camera = None
                self._camera_selector.set_active_camera(None)
                self._controls_page.set_camera(None)
                self._preview.show_status(
                    _("No camera"),
                    _("Connect a camera or select one from the list above."),
                )
                self.set_title(APP_NAME)

        # Start background virtual cameras for all detected V4L2 cameras
        # (vcams persist until the app is closed)
        for cam in self._camera_manager.cameras:
            self._stream_engine.ensure_bg_vcam(cam)

        # Show toast for newly connected cameras (deduplicate by name)
        for cam in self._camera_manager.cameras:
            if cam.id not in self._known_camera_ids and cam.name not in self._known_camera_names:
                toast = Adw.Toast.new(f"📷  {cam.name}")
                toast.set_timeout(4)
                toast.set_button_label(_("Show"))
                toast.connect(
                    "button-clicked",
                    lambda _t, c=cam: self._select_camera_by_id(c.id),
                )
                self._toast_overlay.add_toast(toast)

        self._known_camera_ids = current_ids
        self._known_camera_names = {c.name for c in self._camera_manager.cameras}

        if self._active_camera is None and self._camera_manager.cameras:
            last_id = self._settings.get("last-camera-id")
            cam = None
            if last_id:
                cam = next(
                    (c for c in self._camera_manager.cameras if c.id == last_id),
                    None,
                )
            if cam is None:
                cam = self._camera_manager.cameras[0]
            # Sync dropdown silently (no signal) then start camera directly
            cameras = self._camera_manager.cameras
            for i, c in enumerate(cameras):
                if c.id == cam.id:
                    self._camera_selector.set_selected_silent(i)
                    break
            self._on_camera_selected(self._camera_selector, cam)

        elif not self._camera_manager.cameras:
            # All cameras disconnected — stop stream and reset UI
            log.info("All cameras removed — stopping stream and resetting UI")
            self._stream_engine.stop()
            self._stream_engine.stop_all_bg_vcams()
            self._active_camera = None
            self._camera_selector.set_active_camera(None)
            self._controls_page.set_camera(None)
            self._preview.show_status(
                _("No camera"),
                _("Connect a camera or select one from the list above."),
            )
            self.set_title(APP_NAME)

        elif (
            self._active_camera
            and self._active_camera.id not in current_ids
            and self._camera_manager.cameras
        ):
            # Active camera was disconnected but others remain — switch to first available
            log.info("Active camera %s disconnected, switching to %s",
                      self._active_camera.name, self._camera_manager.cameras[0].name)
            # Stop old camera's backend (kills gphoto2/ffmpeg processes)
            self._stream_engine.stop()
            self._active_camera = None
            cam = self._camera_manager.cameras[0]
            cameras = self._camera_manager.cameras
            for i, c in enumerate(cameras):
                if c.id == cam.id:
                    self._camera_selector.set_selected_silent(i)
                    break
            self._on_camera_selected(self._camera_selector, cam)

    def _select_camera_by_id(self, camera_id: str) -> None:
        """Select a camera by its ID in the dropdown and start preview."""
        cameras = self._camera_manager.cameras
        for i, cam in enumerate(cameras):
            if cam.id == camera_id:
                self._camera_selector.set_selected_silent(i)
                self._on_camera_selected(self._camera_selector, cam)
                return

    def _on_window_mapped(self, _window: Adw.ApplicationWindow) -> None:
        """Restart hotplug when window becomes visible again after background mode."""
        if self._settings.get("hotplug_enabled"):
            self._camera_manager.start_hotplug()

    def _on_close(self, _window: Adw.ApplicationWindow) -> bool:
        if self._closing:
            return True
        # Gather ALL active camera sources
        active_names: list[str] = []

        # Main camera (playing)
        if self._stream_engine.is_playing() and self._active_camera:
            active_names.append(self._active_camera.name)

        # Background virtual cameras (cameras kept on after switching)
        for cam in self._camera_manager.cameras:
            if cam.id in self._stream_engine._bg_vcam_pipelines or cam.id in self._stream_engine._bg_vcam_feeders:
                if cam.name not in active_names:
                    active_names.append(cam.name)

        # Phone camera services
        ctrl = self._mobile_device_ctrl
        if ctrl.phone_server and ctrl.phone_server.running:
            label = _("Phone (Browser Wi-Fi)")
            if label not in active_names:
                active_names.append(label)
        if (ctrl.scrcpy_usb and ctrl.scrcpy_usb.running) or (ctrl.scrcpy_wifi and ctrl.scrcpy_wifi.running):
            label = _("Phone (USB/Wi-Fi scrcpy)")
            if label not in active_names:
                active_names.append(label)
        if ctrl.airplay_receiver and ctrl.airplay_receiver.running:
            label = _("Phone (AirPlay)")
            if label not in active_names:
                active_names.append(label)

        has_active = bool(active_names) or self._stream_engine.has_active_bg_vcams() or self._video_recorder.is_recording or self._video_recorder.is_finalizing

        if has_active:
            parts = []
            if active_names:
                cam_list = "\n".join(f"  • {n}" for n in active_names)
                parts.append(_("Active cameras:") + "\n" + cam_list)
            if VirtualCamera.is_enabled():
                parts.append(_("Virtual Camera is enabled (other apps may depend on it)."))
            parts.append(
                _(
                    "If you choose to keep it running, "
                    "the camera will remain on after closing the application."
                )
            )
            body = "\n\n".join(parts)

            dialog = Adw.AlertDialog.new(_("Camera is active"), body)
            dialog.add_response("stop", _("Stop camera and close"))
            dialog.add_response("keep", _("Keep camera on"))
            dialog.add_response("cancel", _("Cancel"))
            dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_response_appearance("keep", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")
            dialog.connect("response", self._on_close_response)
            self._immersion.present_dialog(dialog, self)
            return True  # block close

        # No active pipeline — hide immediately and clean up
        self.set_visible(False)
        self._cleanup_and_close()
        return True

    def _on_close_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response == "cancel":
            return
        if response == "stop":
            # Hide window immediately so the user sees it close instantly
            self.set_visible(False)
            self._cleanup_and_close()
        else:  # keep — hide window, keep pipeline alive
            self._camera_manager.stop_hotplug()
            self._background_mode = True
            app = self.get_application()
            if app is not None:
                app.hold()
            self.set_visible(False)

    def _cleanup_and_close(self):
        if self._closing:
            return
        self._closing = True
        self._selection_generation += 1
        app = self.get_application()
        app.hold()
        self._close_hold = True
        self._preview.cancel_countdown()
        self._preview.cleanup()
        self._controls_page.cleanup()
        self._settings_page.cleanup()
        self._effects_page.cleanup()
        self._gallery.cleanup()
        self._video_gallery.cleanup()
        self._immersion.cleanup()
        self._resource_monitor.stop()
        self._video_recorder.stop()
        self._audio_monitor.stop_all()
        self._camera_manager.close()
        self._stream_engine.stop(stop_backend=False)
        self._stream_engine.stop_all_bg_vcams()
        self._mobile_device_ctrl.disconnect_signals()
        def cleanup():
            # Recording owns a non-daemon finite worker. Native still capture is also
            # allowed to finish before its camera session is torn down.
            if not self._video_recorder.wait_finalize(25):
                raise RuntimeError("Recording is still finalizing")
            deadline = time.monotonic() + 70
            while self._capture_pending and time.monotonic() < deadline:
                time.sleep(0.1)
            ctrl = self._mobile_device_ctrl
            for service in (ctrl.scrcpy_usb, ctrl.scrcpy_wifi, ctrl.airplay_receiver, ctrl.phone_server):
                service.stop()
            gp_backend = self._camera_manager.get_backend(BackendType.GPHOTO2)
            if gp_backend:
                gp_backend.stop_streaming()
            VirtualCamera.stop()
            VirtualCamera.cleanup_dynamic_devices()
        def done(_result=None):
            if self._recording_hold:
                self._recording_hold = False
                app.release()
            if getattr(self, "_background_mode", False):
                self._background_mode = False
                app.release()
            if self._close_hold:
                self._close_hold = False
                app.release()
            self.destroy()
        def failed(exc):
            # Never discard a still-finalizing recording just to close quickly.
            log.error("Cleanup failed: %s", exc)
            if not self._video_recorder.wait_finalize(0):
                GLib.timeout_add_seconds(1, check_finalized)
            else:
                done()
        def check_finalized():
            if not self._video_recorder.wait_finalize(0):
                return GLib.SOURCE_CONTINUE
            run_async(cleanup, on_success=done, on_error=lambda exc: done())
            return GLib.SOURCE_REMOVE
        run_async(cleanup, on_success=done, on_error=failed)

    # -- theme ---------------------------------------------------------------

    def _apply_theme(self) -> None:
        theme = self._settings.get("theme")
        style_manager = Adw.StyleManager.get_default()
        scheme_map = {
            "system": Adw.ColorScheme.DEFAULT,
            "light": Adw.ColorScheme.FORCE_LIGHT,
            "dark": Adw.ColorScheme.FORCE_DARK,
        }
        style_manager.set_color_scheme(scheme_map.get(theme, Adw.ColorScheme.DEFAULT))

    # -- resource monitor ----------------------------------------------------

    def _on_high_resource(self, _monitor, snapshot, features) -> None:
        """Show resource warning dialog or auto-optimize when usage is high."""
        if self._settings.get("resource-monitor-auto-optimize", False):
            # Auto-optimize without showing the dialog
            can_disable = [f for f in features if f.disableable]
            disabled_ids: list[str] = []
            for feat in can_disable:
                try:
                    feat.disable()
                    disabled_ids.append(feat.feature_id)
                except Exception:
                    pass
            if disabled_ids:
                self._sync_ui_after_optimize(disabled_ids)
                toast = Adw.Toast.new(_("High resource usage detected. Optimized automatically."))
                toast.set_timeout(4)
                self._toast_overlay.add_toast(toast)
            return

        show_resource_warning(
            parent=self,
            snapshot=snapshot,
            features=features,
            settings=self._settings,
            present_fn=self._immersion.present_dialog,
            on_optimized=self._sync_ui_after_optimize,
        )

    def _sync_ui_after_optimize(self, disabled_ids: list[str]) -> None:
        """Update UI widgets after the resource dialog disabled features."""
        for fid in disabled_ids:
            if fid == "effects":
                self._effects_page.sync_ui()
            elif fid == "virtual-camera":
                self._settings_page.set_vc_toggle_active(False)
                self._vcam_quick_btn.handler_block(self._vcam_btn_handler_id)
                self._vcam_quick_btn.set_active(False)
                self._vcam_quick_btn.handler_unblock(self._vcam_btn_handler_id)

    def _on_resource_monitor_changed(self, _page, enabled: bool) -> None:
        """Start or stop resource monitor when user toggles the setting."""
        if enabled:
            self._resource_monitor.start()
        else:
            self._resource_monitor.stop()

    # -- immersion -----------------------------------------------------------

    def _setup_immersion(self) -> None:
        """Wire up the immersive auto-hide controller."""
        self._immersion = ImmersionController(self)
        self._immersion.set_split_view(self._split_view)
        self._immersion.set_root_box(self._root_box)

        # Top/bottom bar revealers (crossfade)
        self._immersion.add_revealer(self._top_bar_revealer)
        self._immersion.add_revealer(self._bottom_bar_revealer)

        # Window-level progress bar
        self._immersion.add_fade_widget(self._progress)

        # Preview overlays that should fade
        for w in self._preview.immersion_widgets():
            self._immersion.add_fade_widget(w)

    def _setup_resource_monitor(self) -> None:
        """Register features and start the resource usage monitor."""
        mon = self._resource_monitor
        se = self._stream_engine

        # Register monitorable features
        mon.register_feature(FeatureDescriptor(
            feature_id="effects",
            label=_("Video effects"),
            description=_("Real-time filters, background blur, artistic effects"),
            is_active=lambda: se.effects.has_active_effects(),
            disable=lambda: se.effects.reset_all(),
            estimated_cpu=40.0,
            estimated_ram_mb=150.0,
        ))
        mon.register_feature(FeatureDescriptor(
            feature_id="virtual-camera",
            label=_("Virtual camera"),
            description=_("v4l2loopback output for video conferencing"),
            is_active=lambda: se.vcam_active,
            disable=lambda: se.stop_vcam(),
            estimated_cpu=20.0,
            estimated_ram_mb=80.0,
            disableable=False,
        ))
        mon.register_feature(FeatureDescriptor(
            feature_id="bg-vcam-feeders",
            label=_("Background virtual cameras"),
            description=_("Virtual camera feeds for inactive cameras"),
            is_active=lambda: se.has_active_bg_vcams(),
            disable=lambda: se.stop_all_bg_vcams(),
            estimated_cpu=30.0,
            estimated_ram_mb=100.0,
        ))
        mon.register_feature(FeatureDescriptor(
            feature_id="recording",
            label=_("Video recording"),
            description=_("Active video recording with encoding"),
            is_active=lambda: (
                self._video_recorder is not None
                and self._video_recorder.is_recording
            ),
            disable=lambda: self._on_record_toggle() if self._video_recorder.is_recording else None,
            estimated_cpu=50.0,
            estimated_ram_mb=150.0,
            disableable=False,
        ))
        mon.register_feature(FeatureDescriptor(
            feature_id="phone-server",
            label=_("Phone camera server"),
            description=_("HTTPS/WebSocket server for phone camera"),
            is_active=lambda: self._mobile_device_ctrl.phone_server.running,
            disable=lambda: None,
            estimated_cpu=15.0,
            estimated_ram_mb=50.0,
            disableable=False,
        ))
        mon.register_feature(FeatureDescriptor(
            feature_id="scrcpy",
            label=_("Scrcpy (Android camera)"),
            description=_("Android camera via USB/Wi-Fi ADB"),
            is_active=lambda: self._mobile_device_ctrl.scrcpy_usb.running or self._mobile_device_ctrl.scrcpy_wifi.running,
            disable=lambda: (self._mobile_device_ctrl.scrcpy_usb.stop(), self._mobile_device_ctrl.scrcpy_wifi.stop()),
            estimated_cpu=40.0,
            estimated_ram_mb=200.0,
            disableable=False,
        ))
        mon.register_feature(FeatureDescriptor(
            feature_id="airplay",
            label=_("AirPlay receiver"),
            description=_("Apple AirPlay screen mirroring via UxPlay"),
            is_active=lambda: self._mobile_device_ctrl.airplay_receiver.running,
            disable=lambda: None,
            estimated_cpu=40.0,
            estimated_ram_mb=200.0,
            disableable=False,
        ))

        mon.connect("high-resource", self._on_high_resource)

        if self._settings.get(MONITOR_ENABLED_KEY):
            mon.start()
