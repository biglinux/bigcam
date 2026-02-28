"""Main application window – Paned layout with preview + controls sidebar."""

from __future__ import annotations

import os
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, Gio, GLib

from constants import APP_NAME
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
from ui.virtual_camera_page import VirtualCameraPage
from ui.settings_page import SettingsPage
from ui.about_dialog import show_about
from ui.ip_camera_dialog import IPCameraDialog
from utils.settings_manager import SettingsManager
from utils.async_worker import run_async
from utils.i18n import _


class BigDigicamWindow(Adw.ApplicationWindow):
    """Primary window with preview pane and tabbed control sidebar."""

    def __init__(self, app: Adw.Application) -> None:
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(1000, 650)
        self.set_size_request(700, 500)

        self._settings = SettingsManager()
        self._camera_manager = CameraManager()
        self._stream_engine = StreamEngine(self._camera_manager)
        self._stream_engine.mirror = bool(self._settings.get("mirror_preview"))
        self._photo_capture = PhotoCapture(self._camera_manager)
        self._video_recorder = VideoRecorder(self._camera_manager)

        self._active_camera: CameraInfo | None = None
        self._streaming_lock = threading.Lock()

        self._build_ui()
        self._setup_actions()
        self._setup_shortcuts()
        self._connect_signals()
        self._apply_theme()

        # Initial camera detection
        GLib.idle_add(self._camera_manager.detect_cameras_async)

        if self._settings.get("hotplug_enabled"):
            self._camera_manager.start_hotplug()

    # -- UI build ------------------------------------------------------------

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Header bar
        self._header = Adw.HeaderBar()
        self._header.set_title_widget(Adw.WindowTitle(title=APP_NAME, subtitle=""))

        # Camera selector in header
        self._camera_selector = CameraSelector(self._camera_manager)
        self._header.pack_start(self._camera_selector)

        # Menu button
        menu_btn = Gtk.MenuButton()
        menu_btn.set_icon_name("open-menu-symbolic")
        menu_btn.set_tooltip_text(_("Menu"))
        menu_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Main menu")]
        )
        menu_btn.set_menu_model(self._build_menu())
        self._header.pack_end(menu_btn)

        # Refresh button
        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.set_tooltip_text(_("Refresh cameras"))
        refresh_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Refresh camera list")]
        )
        refresh_btn.set_action_name("win.refresh")
        self._header.pack_end(refresh_btn)

        root.append(self._header)

        # Progress bar (thin, hidden by default)
        self._progress = Gtk.ProgressBar(visible=False)
        self._progress.add_css_class("osd")
        root.append(self._progress)

        # Main content: Paned
        self._paned = Gtk.Paned(
            orientation=Gtk.Orientation.HORIZONTAL,
            shrink_start_child=False,
            shrink_end_child=False,
        )
        self._paned.set_position(600)

        # LEFT: preview
        self._preview = PreviewArea(self._stream_engine)
        self._preview.set_show_fps(self._settings.get("show_fps"))
        self._paned.set_start_child(self._preview)

        # RIGHT: sidebar with ViewStack
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        sidebar.set_size_request(300, -1)

        # ViewSwitcherBar-like header for pages
        self._view_stack = Adw.ViewStack()
        self._view_stack.set_vexpand(True)

        # Controls page
        self._controls_page = CameraControlsPage(self._camera_manager)
        self._view_stack.add_titled_with_icon(
            self._controls_page, "controls",
            _("Controls"), "emblem-system-symbolic",
        )

        # Photo gallery page
        self._gallery = PhotoGallery()
        self._view_stack.add_titled_with_icon(
            self._gallery, "gallery",
            _("Photos"), "image-x-generic-symbolic",
        )

        # Video gallery page
        self._video_gallery = VideoGallery()
        self._view_stack.add_titled_with_icon(
            self._video_gallery, "videos",
            _("Videos"), "video-x-generic-symbolic",
        )

        # Virtual camera page
        self._virtual_page = VirtualCameraPage()
        self._view_stack.add_titled_with_icon(
            self._virtual_page, "virtual",
            _("Virtual Camera"), "camera-video-symbolic",
        )

        # Restore virtual camera enabled state from settings
        if self._settings.get("virtual-camera-enabled"):
            VirtualCamera.set_enabled(True)
            self._virtual_page.set_toggle_active(True)

        # Settings page
        self._settings_page = SettingsPage(self._settings)
        self._view_stack.add_titled_with_icon(
            self._settings_page, "settings",
            _("Settings"), "preferences-system-symbolic",
        )

        switcher = Adw.ViewSwitcherBar(stack=self._view_stack, reveal=True)

        sidebar.append(self._view_stack)
        sidebar.append(switcher)

        self._paned.set_end_child(sidebar)
        root.append(self._paned)

        self.set_content(root)

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
        section3.append(_("Refresh") + " (F5)", "win.refresh")
        section3.append(_("About"), "win.about")
        menu.append_section(None, section3)
        return menu

    # -- actions -------------------------------------------------------------

    def _setup_actions(self) -> None:
        simple_actions = {
            "refresh": self._on_refresh,
            "add-ip": self._on_add_ip,
            "about": self._on_about,
            "capture": self._on_capture_action,
            "record-toggle": self._on_record_toggle,
            "save-profile": self._on_save_profile,
            "load-profile": self._on_load_profile,
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
            "win.capture": ["<Primary>p", "space"],
            "win.record-toggle": ["<Primary>r"],
            "win.refresh": ["F5", "<Primary>F5"],
            "win.save-profile": ["<Primary>s"],
            "win.load-profile": ["<Primary>l"],
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
        self._camera_manager.connect("cameras-changed", self._on_cameras_changed_auto_start)
        self._virtual_page.connect("virtual-camera-toggled", self._on_virtual_camera_toggled)
        self._settings_page.connect("show-fps-changed", self._on_show_fps_changed)
        self._settings_page.connect("mirror-changed", self._on_mirror_changed)
        self.connect("close-request", self._on_close)

    # -- signal handlers -----------------------------------------------------

    def _on_camera_selected(self, _selector: CameraSelector, camera: CameraInfo) -> None:
        self._active_camera = camera
        title_widget = self._header.get_title_widget()
        if isinstance(title_widget, Adw.WindowTitle):
            title_widget.set_subtitle(camera.name)

        # Check if backend needs streaming setup (e.g. gphoto2)
        backend = self._camera_manager.get_backend(camera.backend)
        needs_setup = (backend and hasattr(backend, "needs_streaming_setup")
                       and backend.needs_streaming_setup())

        print(f"[DEBUG] Camera selected: {camera.name}, backend={camera.backend}, needs_setup={needs_setup}")

        if needs_setup:
            # Prevent concurrent streaming attempts — ignore if already in progress
            if self._streaming_lock.locked():
                print("[DEBUG] Streaming already in progress, ignoring selection")
                return

            # Stop hotplug polling to prevent gphoto2 --auto-detect racing with streaming
            self._camera_manager.stop_hotplug()

            self._stream_engine.stop()
            self._preview.notification.notify_user(
                _("Starting camera stream…"), "info", 0
            )

            def do_controls_then_stream() -> tuple[bool, list]:
                """Fetch controls BEFORE streaming (gphoto2 locks USB)."""
                if not self._streaming_lock.acquire(blocking=False):
                    print("[DEBUG] Lock already held, aborting")
                    return False, []
                try:
                    # Fetch controls while camera USB is free
                    print("[DEBUG] Fetching gPhoto2 controls before streaming...")
                    controls = self._camera_manager.get_controls(camera)
                    print(f"[DEBUG] Got {len(controls)} controls")

                    # Now start streaming (takes over USB)
                    print("[DEBUG] Starting streaming...")
                    success = backend.start_streaming(camera)
                    print(f"[DEBUG] Streaming result: {success}")
                    if success:
                        GLib.idle_add(
                            lambda: self._preview.notification.notify_user(
                                _("Camera streaming started!"), "success", 3000
                            ) or False
                        )
                    return success, controls
                finally:
                    self._streaming_lock.release()

            def on_done(result: tuple[bool, list]) -> None:
                success, controls = result
                print(f"[DEBUG] on_done: success={success}, controls={len(controls)}")
                self._preview.notification.dismiss()
                if success:
                    self._controls_page.set_camera_with_controls(camera, controls)
                    self._stream_engine.play(camera, streaming_ready=True)
                else:
                    self._preview.notification.notify_user(
                        _("Failed to start camera streaming."), "error"
                    )
                    self._preview._show_retry()

            run_async(do_controls_then_stream, on_success=on_done)
        else:
            # V4L2, libcamera, PipeWire: load controls async + start stream
            self._controls_page.set_camera(camera)
            self._stream_engine.play(camera)

    def _on_retry(self, _preview: PreviewArea) -> None:
        """Re-attempt camera connection when user clicks Try Again."""
        if self._active_camera:
            self._stream_engine.stop()
            self._on_camera_selected(self._camera_selector, self._active_camera)

    def _on_virtual_camera_toggled(self, _page, _enabled: bool) -> None:
        """Restart stream to add/remove virtual camera loopback output."""
        self._settings.set("virtual-camera-enabled", _enabled)
        if self._active_camera:
            self._stream_engine.stop()
            self._on_camera_selected(self._camera_selector, self._active_camera)

    def _on_show_fps_changed(self, _page, show: bool) -> None:
        self._preview.set_show_fps(show)

    def _on_mirror_changed(self, _page, mirror: bool) -> None:
        self._stream_engine.mirror = mirror
        if self._active_camera:
            self._stream_engine.stop()
            self._on_camera_selected(self._camera_selector, self._active_camera)

    def _on_capture(self, _preview: PreviewArea) -> None:
        if not self._active_camera:
            self._preview.notification.notify_user(
                _("No camera selected."), "warning"
            )
            return

        self._preview.notification.notify_user(
            _("Capturing photo…"), "info", 1500
        )

        import time as _time
        from utils import xdg

        timestamp = _time.strftime("%Y%m%d_%H%M%S")
        output_dir = xdg.photos_dir()
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"bigcam_{timestamp}.png")

        ok = self._stream_engine.capture_snapshot(output_path)
        if ok:
            self._preview.notification.notify_user(
                _("Photo saved!"), "success"
            )
            self._gallery.refresh()
        else:
            self._preview.notification.notify_user(
                _("Failed to capture photo."), "error"
            )

    def _on_refresh(self, *_args) -> None:
        self._camera_manager.detect_cameras_async()

    def _on_add_ip(self, *_args) -> None:
        dialog = IPCameraDialog()
        dialog.connect("camera-added", self._on_ip_camera_added)
        dialog.present(self)

    def _on_ip_camera_added(self, _dialog: IPCameraDialog, name: str, url: str) -> None:
        ip_list = self._settings.get("ip_cameras")
        if not isinstance(ip_list, list):
            ip_list = list(ip_list) if ip_list else []
        ip_list.append({"name": name, "url": url})
        self._settings.set("ip_cameras", ip_list)
        self._camera_manager.add_ip_cameras(ip_list)

    def _on_about(self, *_args) -> None:
        show_about(self)

    def _on_camera_error(self, _manager: CameraManager, message: str) -> None:
        self._preview.notification.notify_user(message, "error", 5000)

    # -- recording -----------------------------------------------------------

    def _on_capture_action(self, *_args) -> None:
        self._on_capture(self._preview)

    def _on_record_toggle(self, *_args) -> None:
        if self._video_recorder.is_recording:
            path = self._video_recorder.stop()
            self._preview.set_recording_state(False)
            if path:
                self._preview.notification.notify_user(
                    _("Video saved: %s") % os.path.basename(path), "success"
                )
                self._video_gallery.refresh()
        else:
            if not self._active_camera:
                self._preview.notification.notify_user(
                    _("No camera selected."), "warning"
                )
                return
            path = self._video_recorder.start(
                self._active_camera, self._stream_engine.pipeline,
            )
            if path:
                self._preview.set_recording_state(True)
                self._preview.notification.notify_user(
                    _("Recording…"), "info", 0
                )
            else:
                self._preview.notification.notify_user(
                    _("Failed to start recording."), "error"
                )

    # -- profiles ------------------------------------------------------------

    def _on_save_profile(self, *_args) -> None:
        if not self._active_camera:
            return
        controls = self._camera_manager.get_controls(self._active_camera)
        if not controls:
            return
        # Use camera name as default profile
        name = "default"
        camera_profiles.save_profile(self._active_camera, name, controls)
        self._preview.notification.notify_user(
            _("Profile saved."), "success"
        )

    def _on_load_profile(self, *_args) -> None:
        if not self._active_camera:
            return
        profiles = camera_profiles.list_profiles(self._active_camera)
        if not profiles:
            self._preview.notification.notify_user(
                _("No profiles found."), "info"
            )
            return
        # Load the first available profile (default)
        name = profiles[0]
        values = camera_profiles.load_profile(self._active_camera, name)
        for ctrl_id, value in values.items():
            self._camera_manager.set_control(self._active_camera, ctrl_id, value)
        # Refresh controls UI
        self._controls_page.set_camera(self._active_camera)
        self._preview.notification.notify_user(
            _("Profile loaded: %s") % name, "success"
        )

    # -- auto-start preview --------------------------------------------------

    def _on_cameras_changed_auto_start(self, _manager: CameraManager) -> None:
        """Auto-start preview with the first camera if none is active."""
        if self._active_camera is None and self._camera_manager.cameras:
            cam = self._camera_manager.cameras[0]
            self._on_camera_selected(self._camera_selector, cam)

    def _on_close(self, _window: Adw.ApplicationWindow) -> bool:
        self._video_recorder.stop()
        self._stream_engine.stop()
        self._camera_manager.stop_hotplug()
        VirtualCamera.stop()
        return False

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
