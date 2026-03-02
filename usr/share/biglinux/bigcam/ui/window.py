"""Main application window – Paned layout with preview + controls sidebar."""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, Gio, GLib  # noqa: E402

log = logging.getLogger(__name__)

from constants import APP_NAME, BackendType  # noqa: E402
from core.camera_backend import CameraInfo  # noqa: E402
from core.camera_manager import CameraManager  # noqa: E402
from core.stream_engine import StreamEngine  # noqa: E402
from core.photo_capture import PhotoCapture  # noqa: E402
from core.video_recorder import VideoRecorder  # noqa: E402
from core.virtual_camera import VirtualCamera  # noqa: E402
from core import camera_profiles  # noqa: E402
from ui.preview_area import PreviewArea  # noqa: E402
from ui.camera_controls_page import CameraControlsPage  # noqa: E402
from ui.camera_selector import CameraSelector  # noqa: E402
from ui.photo_gallery import PhotoGallery  # noqa: E402
from ui.video_gallery import VideoGallery  # noqa: E402
from ui.settings_page import SettingsPage  # noqa: E402
from ui.effects_page import EffectsPage  # noqa: E402
from ui.about_dialog import show_about  # noqa: E402
from ui.ip_camera_dialog import IPCameraDialog  # noqa: E402
from ui.phone_camera_dialog import PhoneCameraDialog  # noqa: E402
from core.phone_camera import PhoneCameraServer  # noqa: E402
from utils.settings_manager import SettingsManager  # noqa: E402
from utils.async_worker import run_async  # noqa: E402
from utils.i18n import _  # noqa: E402


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
        self._stream_engine._video_recorder = self._video_recorder

        self._active_camera: CameraInfo | None = None
        self._known_camera_ids: set[str] = set()
        self._controls_cache: dict[str, list] = {}
        self._streaming_lock = threading.Lock()
        self._phone_server = PhoneCameraServer()
        self._phone_server.connect("connected", self._on_phone_connected)
        self._phone_server.connect("disconnected", self._on_phone_disconnected)

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
        menu_btn.update_property([Gtk.AccessibleProperty.LABEL], [_("Main menu")])
        menu_btn.set_menu_model(self._build_menu())
        self._header.pack_end(menu_btn)

        # Phone camera button with status dot overlay
        phone_btn = Gtk.Button.new_from_icon_name("phone-symbolic")
        phone_btn.set_tooltip_text(_("Phone as Webcam"))
        phone_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Phone as Webcam")]
        )
        phone_btn.set_action_name("win.phone-camera")

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
        self._phone_dot.set_visible(False)

        phone_overlay = Gtk.Overlay()
        phone_overlay.set_child(phone_btn)
        phone_overlay.add_overlay(self._phone_dot)
        self._header.pack_end(phone_overlay)

        self._phone_server.connect("status-changed", self._on_phone_status_dot)

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
        self._preview.set_grid_visible(self._settings.get("grid_overlay"))
        self._preview.set_mirror(bool(self._settings.get("mirror_preview")))
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
            self._controls_page,
            "controls",
            _("Controls"),
            "adjustlevels",
        )

        # Effects page
        self._effects_page = EffectsPage(self._stream_engine.effects)
        self._view_stack.add_titled_with_icon(
            self._effects_page,
            "effects",
            _("Effects"),
            "draw-watercolor",
        )

        # Photo gallery page
        self._gallery = PhotoGallery()
        self._view_stack.add_titled_with_icon(
            self._gallery,
            "gallery",
            _("Photos"),
            "view-list-images",
        )

        # Video gallery page
        self._video_gallery = VideoGallery()
        self._view_stack.add_titled_with_icon(
            self._video_gallery,
            "videos",
            _("Videos"),
            "view-list-video",
        )

        # Settings page (includes Tools and Virtual Camera)
        self._settings_page = SettingsPage(self._settings, self._stream_engine)
        self._settings_page.connect("smile-captured", self._on_smile_captured)
        self._settings_page.connect("qr-detected", self._on_qr_detected)
        self._settings_page.connect(
            "virtual-camera-toggled", self._on_virtual_camera_toggled
        )
        self._settings_page.connect("resolution-changed", self._on_resolution_changed)
        self._settings_page.connect("fps-limit-changed", self._on_fps_limit_changed)
        self._settings_page.connect(
            "grid-overlay-changed", self._on_grid_overlay_changed
        )

        # Restore virtual camera enabled state from settings
        if self._settings.get("virtual-camera-enabled"):
            VirtualCamera.set_enabled(True)
            self._settings_page.set_vc_toggle_active(True)

        self._view_stack.add_titled_with_icon(
            self._settings_page,
            "settings",
            _("Settings"),
            "configure",
        )

        switcher = Adw.ViewSwitcherBar(stack=self._view_stack, reveal=True)

        sidebar.append(self._view_stack)
        sidebar.append(switcher)

        self._paned.set_end_child(sidebar)
        root.append(self._paned)

        from ui.notification import InlineNotification

        self._notification = InlineNotification()

        # Wrap root in a vertical box with notification at top
        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        wrapper.append(self._notification)
        wrapper.append(root)
        root.set_vexpand(True)
        self.set_content(wrapper)

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
        section3.append(_("About"), "win.about")
        menu.append_section(None, section3)
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
        self._camera_manager.connect(
            "cameras-changed", self._on_cameras_changed_auto_start
        )
        self._settings_page.connect("show-fps-changed", self._on_show_fps_changed)
        self._settings_page.connect("mirror-changed", self._on_mirror_changed)
        self.connect("close-request", self._on_close)

    # -- signal handlers -----------------------------------------------------

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
        self._active_camera = camera
        self._settings.set("last-camera-id", camera.id)
        title_widget = self._header.get_title_widget()
        if isinstance(title_widget, Adw.WindowTitle):
            title_widget.set_subtitle(camera.name)

        # Check if backend needs streaming setup (e.g. gphoto2)
        backend = self._camera_manager.get_backend(camera.backend)
        needs_setup = (
            backend
            and hasattr(backend, "needs_streaming_setup")
            and backend.needs_streaming_setup()
        )

        log.debug(
            "Camera selected: %s, backend=%s, needs_setup=%s",
            camera.name,
            camera.backend,
            needs_setup,
        )
        if needs_setup:
            # Prevent concurrent streaming attempts — ignore if already in progress
            if self._streaming_lock.locked():
                log.debug("Streaming already in progress, ignoring selection")
                return

            # Stop hotplug polling to prevent gphoto2 --auto-detect racing with streaming
            self._camera_manager.stop_hotplug()

            # Check if this camera already has a streaming session alive
            already_streaming = hasattr(
                backend, "is_camera_streaming"
            ) and backend.is_camera_streaming(camera)
            cached_controls = self._controls_cache.get(camera.id)

            log.debug(
                "already_streaming=%s, cached_controls=%s, camera.id=%s",
                already_streaming,
                cached_controls is not None,
                camera.id,
            )
            if hasattr(backend, "_active_streams"):
                log.debug("_active_streams=%s", dict(backend._active_streams))
            if already_streaming and cached_controls is not None:
                # Hot-swap: camera already streaming, just switch the GStreamer pipeline
                log.debug("Hot-swap to %s (already streaming)", camera.name)
                self._stream_engine.stop(stop_backend=False)
                self._controls_page.set_camera_with_controls(camera, cached_controls)
                self._stream_engine.play(camera, streaming_ready=True)
                return

            # Stop only the GStreamer pipeline, keep other cameras' backend alive
            self._stream_engine.stop(stop_backend=False)
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
                    controls = cached_controls
                    if controls is None:
                        # Fetch controls while camera USB is free
                        log.debug("Fetching gPhoto2 controls before streaming...")
                        controls = self._camera_manager.get_controls(camera)
                        log.debug("Got %s controls", len(controls))
                    if already_streaming:
                        log.debug("Camera already streaming, skipping start")
                        return True, controls

                    # Now start streaming (takes over USB)
                    log.debug("Starting streaming...")
                    success = backend.start_streaming(camera)
                    log.debug("Streaming result: %s", success)
                    if success:
                        GLib.idle_add(
                            lambda: (
                                self._preview.notification.notify_user(
                                    _("Camera streaming started!"), "success", 3000
                                )
                                or False
                            )
                        )
                    return success, controls
                finally:
                    self._streaming_lock.release()

            def on_done(result: tuple[bool, list]) -> None:
                success, controls = result
                log.debug("on_done: success=%s, controls=%s", success, len(controls))
                self._preview.notification.dismiss()
                if success:
                    self._controls_cache[camera.id] = controls
                    self._controls_page.set_camera_with_controls(camera, controls)
                    self._stream_engine.play(camera, streaming_ready=True)
                else:
                    self._preview.notification.notify_user(
                        _("Failed to start camera streaming."), "error"
                    )
                    self._preview._show_retry()
                # Restart hotplug after gPhoto2 streaming setup completes
                if self._settings.get("hotplug_enabled"):
                    self._camera_manager.start_hotplug()

            run_async(do_controls_then_stream, on_success=on_done)
        else:
            # V4L2, libcamera, PipeWire: load controls async + start stream
            # Stop only GStreamer pipeline on UI thread (instant)
            old_camera = self._stream_engine._current_camera
            old_backend_obj = None
            if old_camera and old_camera.backend != camera.backend:
                old_backend_obj = self._camera_manager.get_backend(old_camera.backend)
            self._stream_engine.stop(stop_backend=False)

            # Start the V4L2 camera immediately
            self._controls_page.set_camera(camera)
            preferred_fmt = self._pick_preferred_format(camera)
            self._stream_engine.play(camera, fmt=preferred_fmt)

            # Stop old backend (gphoto2) in background — has time.sleep() calls
            if old_backend_obj and hasattr(old_backend_obj, "stop_streaming"):
                run_async(lambda: old_backend_obj.stop_streaming(old_camera))

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
        self._preview.set_mirror(mirror)

    def _on_resolution_changed(self, _page, value: str) -> None:
        if self._active_camera:
            self._stream_engine.stop()
            self._on_camera_selected(self._camera_selector, self._active_camera)

    def _on_fps_limit_changed(self, _page, value: int) -> None:
        if self._active_camera:
            self._stream_engine.stop()
            self._on_camera_selected(self._camera_selector, self._active_camera)

    def _on_grid_overlay_changed(self, _page, visible: bool) -> None:
        self._preview.set_grid_visible(visible)

    # -- Tools signals -------------------------------------------------------

    def _on_smile_captured(self, _page: Any, path: str) -> None:
        self._preview.notification.notify_user(
            _("Smile captured! Photo saved."), "success", 3000
        )
        self._gallery.refresh()

    def _on_qr_detected(self, _page: Any, text: str) -> None:
        self._preview.notification.notify_user(_("QR Code detected!"), "info", 2000)

    # -- Capture -------------------------------------------------------------

    def _on_capture(self, _preview: PreviewArea) -> None:
        if not self._active_camera:
            self._preview.notification.notify_user(_("No camera selected."), "warning")
            return

        timer = self._settings.get("capture-timer")
        if timer and timer > 0:
            self._preview.start_countdown(timer, self._do_capture_after_timer)
            return

        self._do_capture_after_timer()

    def _do_capture_after_timer(self) -> None:
        from constants import BackendType

        if self._active_camera and self._active_camera.backend == BackendType.GPHOTO2:
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
            dialog.set_close_response("webcam")
            dialog.connect("response", self._on_capture_mode_response)
            dialog.present(self)
            return

        self._do_webcam_capture()

    def _on_capture_mode_response(
        self, _dialog: Adw.AlertDialog, response: str
    ) -> None:
        if response == "native":
            self._do_native_capture()
        else:
            self._do_webcam_capture()

    def _do_webcam_capture(self) -> None:
        self._preview.notification.notify_user(_("Capturing photo…"), "info", 1500)

        import time as _time
        from utils import xdg

        timestamp = _time.strftime("%Y%m%d_%H%M%S")
        output_dir = xdg.photos_dir()
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"bigcam_{timestamp}.png")

        ok = self._stream_engine.capture_snapshot(output_path)
        if ok:
            self._preview.notification.notify_user(_("Photo saved!"), "success")
            self._gallery.refresh()
        else:
            self._preview.notification.notify_user(
                _("Failed to capture photo."), "error"
            )

    def _do_native_capture(self) -> None:
        camera = self._active_camera
        if not camera:
            return

        # Show waiting state in preview
        self._stream_engine.stop()
        self._preview.show_status(
            _("Please wait…"),
            _("Switching to photography mode."),
            "camera-photo-symbolic",
            loading=True,
        )

        def _capture_in_thread() -> str | None:
            import time as _time
            from utils import xdg

            self._camera_manager.get_backend(camera.backend).stop_streaming()

            timestamp = _time.strftime("%Y%m%d_%H%M%S")
            output_dir = xdg.photos_dir()
            os.makedirs(output_dir, exist_ok=True)
            output_path = os.path.join(output_dir, f"bigcam_{timestamp}.jpg")

            ok = self._camera_manager.capture_photo(camera, output_path)
            return output_path if ok else None

        def _on_done(result: str | None) -> None:
            if result:
                self._preview.notification.notify_user(_("Photo saved!"), "success")
                self._gallery.refresh()
            else:
                self._preview.notification.notify_user(
                    _("Failed to capture photo."), "error"
                )
            # Resume streaming
            self._preview.show_status(
                _("Please wait…"),
                _("Resuming camera streaming…"),
                "camera-web-symbolic",
                loading=True,
            )
            self._on_camera_selected(self._camera_selector, camera)

        run_async(_capture_in_thread, on_success=_on_done)

    def _on_refresh(self, *_args) -> None:
        self._camera_manager.detect_cameras_async()

    def _on_add_ip(self, *_args) -> None:
        dialog = IPCameraDialog()
        dialog.connect("camera-added", self._on_ip_camera_added)
        dialog.present(self)

    def _draw_phone_dot(self, area: Gtk.DrawingArea, cr, w: int, h: int) -> None:
        """Draw a colored status dot on the phone button."""
        r, g, b = self._phone_status_color
        cr.set_source_rgb(r, g, b)
        cr.arc(w / 2, h / 2, min(w, h) / 2, 0, 2 * 3.14159265)
        cr.fill()

    def _on_phone_status_dot(self, _server, status: str) -> None:
        """Update the phone button status dot color."""
        colors = {
            "listening": (1.0, 0.76, 0.03),  # yellow/amber
            "connected": (0.16, 0.65, 0.27),  # green
            "stopped": (0.6, 0.6, 0.6),  # grey
        }
        self._phone_status_color = colors.get(status, (0.6, 0.6, 0.6))
        self._phone_dot.set_visible(status != "stopped")
        self._phone_dot.queue_draw()

    def _on_phone_camera(self, *_args) -> None:
        dialog = PhoneCameraDialog(self._phone_server)
        dialog.present(self)

    def _on_phone_disconnected(self, _server: PhoneCameraServer) -> None:
        """Remove phone camera after a delay (allows reconnection on rotation)."""
        # Cancel previous pending disconnect
        if hasattr(self, "_phone_disconnect_timer") and self._phone_disconnect_timer:
            GLib.source_remove(self._phone_disconnect_timer)
        self._phone_disconnect_timer = GLib.timeout_add_seconds(
            5, self._do_phone_disconnect
        )

    def _do_phone_disconnect(self) -> bool:
        """Actually remove the phone camera after the grace period."""
        self._phone_disconnect_timer = None
        # Check if phone reconnected during the delay
        if self._phone_server and self._phone_server.is_connected:
            return False
        if self._active_camera and self._active_camera.backend == BackendType.PHONE:
            self._stream_engine.stop()
            self._active_camera = None
        self._camera_manager.remove_phone_camera()
        return False

    def _on_phone_connected(
        self, _server: PhoneCameraServer, width: int, height: int
    ) -> None:
        """Register the phone camera as a selectable source."""
        # Cancel pending disconnect if phone reconnected quickly (rotation)
        if hasattr(self, "_phone_disconnect_timer") and self._phone_disconnect_timer:
            GLib.source_remove(self._phone_disconnect_timer)
            self._phone_disconnect_timer = None

        phone_cam = CameraInfo(
            id="phone:websocket",
            name="BigCam Phone",
            backend=BackendType.PHONE,
            device_path="websocket",
            capabilities=["video"],
            extra={"phone_server": self._phone_server},
        )
        self._camera_manager.add_phone_camera(phone_cam)

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
                self._active_camera,
                self._stream_engine.pipeline,
                mirror=self._stream_engine.mirror,
            )
            if path:
                self._preview.set_recording_state(True)
                self._preview.notification.notify_user(
                    _("Recording…"), "info", 0, progress=True
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
        self._preview.notification.notify_user(_("Profile saved."), "success")

    def _on_load_profile(self, *_args) -> None:
        if not self._active_camera:
            return
        profiles = camera_profiles.list_profiles(self._active_camera)
        if not profiles:
            self._preview.notification.notify_user(_("No profiles found."), "info")
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
        """Auto-start preview with the last used camera, or the first available."""
        current_ids = {c.id for c in self._camera_manager.cameras}

        # Show notification for newly connected cameras
        for cam in self._camera_manager.cameras:
            if cam.id not in self._known_camera_ids:
                self._notification.notify_user(
                    f"📷  {cam.name}", level="info", timeout_ms=4000
                )

        self._known_camera_ids = current_ids

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
            self._on_camera_selected(self._camera_selector, cam)

    def _select_camera_by_id(self, camera_id: str) -> None:
        """Select a camera by its ID in the dropdown and start preview."""
        cameras = self._camera_manager.cameras
        for i, cam in enumerate(cameras):
            if cam.id == camera_id:
                self._camera_selector._dropdown.set_selected(i)
                self._on_camera_selected(self._camera_selector, cam)
                return

    def _on_close(self, _window: Adw.ApplicationWindow) -> bool:
        if self._stream_engine.pipeline is not None:
            dialog = Adw.AlertDialog.new(
                _("Camera is active"),
                _(
                    "The camera is currently streaming. "
                    "If you choose to keep it running, "
                    "the camera will remain on after closing the application."
                ),
            )
            dialog.add_response("stop", _("Stop camera and close"))
            dialog.add_response("keep", _("Keep camera on"))
            dialog.add_response("cancel", _("Cancel"))
            dialog.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
            dialog.set_response_appearance("keep", Adw.ResponseAppearance.SUGGESTED)
            dialog.set_default_response("cancel")
            dialog.set_close_response("cancel")
            dialog.connect("response", self._on_close_response)
            dialog.present(self)
            return True  # block close

        self._cleanup_and_close()
        return False

    def _on_close_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response == "cancel":
            return
        if response == "stop":
            self._cleanup_and_close()
        else:  # keep
            self._camera_manager.stop_hotplug()
        self.destroy()

    def _cleanup_and_close(self) -> None:
        self._video_recorder.stop()
        self._stream_engine.stop()
        self._camera_manager.stop_hotplug()
        VirtualCamera.stop()

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
