"""Settings page – global preferences, tools, and virtual camera."""

from __future__ import annotations

import os
import subprocess

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, GLib, GObject

from core.virtual_camera import VirtualCamera
from utils.settings_manager import SettingsManager
from utils import xdg
from utils.i18n import _

try:
    import cv2
    import numpy as np

    _HAS_CV2 = True
except ImportError:
    _HAS_CV2 = False

try:
    import zbar

    _HAS_ZBAR = True
except ImportError:
    _HAS_ZBAR = False

_HAARCASCADES = "/usr/share/opencv4/haarcascades"


class SettingsPage(Gtk.ScrolledWindow):
    """Application-wide preferences using Adw.PreferencesGroup widgets."""

    __gsignals__ = {
        "show-fps-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
        "mirror-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
        "qr-detected": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "virtual-camera-toggled": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
        "resolution-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "fps-limit-changed": (GObject.SignalFlags.RUN_LAST, None, (int,)),
        "grid-overlay-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
        "overlay-opacity-changed": (GObject.SignalFlags.RUN_LAST, None, (int,)),
        "controls-opacity-changed": (GObject.SignalFlags.RUN_LAST, None, (int,)),
        "window-opacity-changed": (GObject.SignalFlags.RUN_LAST, None, (int,)),
        "help-tooltips-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
        "capture-timer-changed": (GObject.SignalFlags.RUN_LAST, None, (int,)),
        "recording-config-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "prefer-v4l2-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
        "resource-monitor-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
    }

    def __init__(self, settings: SettingsManager, stream_engine=None) -> None:
        super().__init__(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        self._settings = settings
        self._engine = stream_engine

        # Tools state
        self._qr_active = False
        self._qr_timer_id: int | None = None
        self._last_qr_text = ""
        self._qr_scanning = False
        self._qr_detector = None
        self._wechat_qr = None
        self._zbar_scanner = None
        self._face_cascade = None

        clamp = Adw.Clamp(maximum_size=600, tightening_threshold=400)
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )

        self._build_general(content)
        self._build_virtual_camera(content)
        self._build_preview(content)
        self._build_recording(content)
        if _HAS_CV2 and stream_engine is not None:
            self._build_tools(content)

        clamp.set_child(content)
        self.set_child(clamp)

    def _build_general(self, content: Gtk.Box) -> None:
        general = Adw.PreferencesGroup(title=_("General"))
        general.set_header_suffix(self._make_group_reset_button(
            _("Reset general settings"), self._on_reset_general,
        ))

        # Photo directory
        photo_row = Adw.ActionRow(
            title=_("Photo directory"),
            subtitle=xdg.photos_dir(),
        )
        photo_row.add_prefix(Gtk.Image.new_from_icon_name("folder-pictures-symbolic"))
        photo_open_btn = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        photo_open_btn.set_valign(Gtk.Align.CENTER)
        photo_open_btn.set_tooltip_text(_("Open photos folder"))
        photo_open_btn.add_css_class("flat")
        photo_open_btn.connect(
            "clicked", lambda _b: self._open_directory(xdg.photos_dir())
        )
        photo_row.add_suffix(photo_open_btn)
        photo_row.set_activatable(False)
        general.add(photo_row)

        # Video directory
        video_row = Adw.ActionRow(
            title=_("Video directory"),
            subtitle=xdg.videos_dir(),
        )
        video_row.add_prefix(Gtk.Image.new_from_icon_name("folder-videos-symbolic"))
        video_open_btn = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        video_open_btn.set_valign(Gtk.Align.CENTER)
        video_open_btn.set_tooltip_text(_("Open videos folder"))
        video_open_btn.add_css_class("flat")
        video_open_btn.connect(
            "clicked", lambda _b: self._open_directory(xdg.videos_dir())
        )
        video_row.add_suffix(video_open_btn)
        video_row.set_activatable(False)
        general.add(video_row)

        # Theme
        self._theme_row = Adw.ComboRow(title=_("Theme"))
        self._theme_row.add_prefix(Gtk.Image.new_from_icon_name("preferences-desktop-appearance-symbolic"))
        theme_model = Gtk.StringList()
        for t in (_("Light"), _("Dark")):
            theme_model.append(t)
        self._theme_row.set_model(theme_model)
        theme_idx = {"light": 0, "dark": 1}.get(
            self._settings.get("theme"), 1
        )
        self._theme_row.set_selected(theme_idx)
        self._theme_row.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Application theme")]
        )
        self._theme_row.connect("notify::selected", self._on_theme)
        general.add(self._theme_row)

        self._hotplug_row = Adw.SwitchRow(
            title=_("USB hotplug detection"),
            subtitle=_("Automatically detect cameras when plugged or unplugged."),
        )
        self._hotplug_row.add_prefix(Gtk.Image.new_from_icon_name("media-removable-symbolic"))
        self._hotplug_row.set_active(self._settings.get("hotplug_enabled"))
        self._hotplug_row.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("USB hotplug detection")]
        )
        self._hotplug_row.connect("notify::active", self._on_hotplug)
        general.add(self._hotplug_row)

        # Help tooltips
        self._help_tooltips_row = Adw.SwitchRow(
            title=_("Show help on hover"),
            subtitle=_("Show tooltip hints when hovering over buttons."),
        )
        self._help_tooltips_row.add_prefix(
            Gtk.Image.new_from_icon_name("dialog-information-symbolic")
        )
        self._help_tooltips_row.set_active(self._settings.get("show-help-tooltips"))
        self._help_tooltips_row.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Show help on hover")]
        )
        self._help_tooltips_row.connect("notify::active", self._on_help_tooltips)
        general.add(self._help_tooltips_row)

        # Resource monitor
        resource_row = Adw.SwitchRow(
            title=_("Resource usage monitor"),
            subtitle=_("Warn when CPU or memory usage is high."),
        )
        resource_row.add_prefix(
            Gtk.Image.new_from_icon_name("utilities-system-monitor-symbolic")
        )
        resource_row.set_active(self._settings.get("resource-monitor-enabled"))
        resource_row.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Resource usage monitor")]
        )
        resource_row.connect("notify::active", self._on_resource_monitor)
        self._resource_row = resource_row
        general.add(resource_row)

        # Reset dismissed warnings
        reset_warnings_row = Adw.ActionRow(
            title=_("Reset resource warnings"),
            subtitle=_("Re-enable all dismissed resource usage warnings."),
        )
        reset_warnings_row.add_prefix(
            Gtk.Image.new_from_icon_name("view-refresh-symbolic")
        )
        reset_btn = Gtk.Button(label=_("Reset"))
        reset_btn.set_valign(Gtk.Align.CENTER)
        reset_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Reset dismissed resource warnings")]
        )
        reset_btn.connect("clicked", self._on_reset_warnings)
        reset_warnings_row.add_suffix(reset_btn)
        reset_warnings_row.set_activatable_widget(reset_btn)
        general.add(reset_warnings_row)

        content.append(general)

    def _build_preview(self, content: Gtk.Box) -> None:
        preview = Adw.PreferencesGroup(title=_("Preview"))
        preview.set_header_suffix(self._make_group_reset_button(
            _("Reset preview settings"), self._on_reset_preview,
        ))

        self._mirror_row = Adw.SwitchRow(
            title=_("Mirror preview"),
            subtitle=_("Flip the preview horizontally like a mirror."),
        )
        self._mirror_row.add_prefix(Gtk.Image.new_from_icon_name("object-flip-horizontal-symbolic"))
        self._mirror_row.set_active(self._settings.get("mirror_preview"))
        self._mirror_row.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Mirror preview")]
        )
        self._mirror_row.connect("notify::active", self._on_mirror)
        preview.add(self._mirror_row)

        self._show_fps_row = Adw.SwitchRow(
            title=_("Show FPS counter"),
        )
        self._show_fps_row.add_prefix(Gtk.Image.new_from_icon_name("preferences-system-symbolic"))
        self._show_fps_row.set_active(self._settings.get("show_fps"))
        self._show_fps_row.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Show FPS counter")]
        )
        self._show_fps_row.connect("notify::active", self._on_show_fps)
        preview.add(self._show_fps_row)

        self._grid_row = Adw.SwitchRow(
            title=_("Grid overlay"),
            subtitle=_("Show a rule-of-thirds grid over the preview."),
        )
        self._grid_row.add_prefix(Gtk.Image.new_from_icon_name("view-grid-symbolic"))
        self._grid_row.set_active(self._settings.get("grid_overlay"))
        self._grid_row.connect("notify::active", self._on_grid_overlay)
        preview.add(self._grid_row)

        # Window background opacity slider
        window_opacity_row = Adw.ActionRow(
            title=_("Background transparency"),
            subtitle=_("Controls the window background transparency."),
        )
        window_opacity_row.add_prefix(Gtk.Image.new_from_icon_name("preferences-desktop-wallpaper-symbolic"))
        self._window_opacity_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 100, 5
        )
        self._window_opacity_scale.set_value(self._settings.get("window-opacity"))
        self._window_opacity_scale.set_hexpand(True)
        self._window_opacity_scale.set_valign(Gtk.Align.CENTER)
        self._window_opacity_scale.set_size_request(180, -1)
        self._window_opacity_scale.connect("value-changed", self._on_window_opacity)
        window_opacity_row.add_suffix(self._window_opacity_scale)
        preview.add(window_opacity_row)

        # Gradient overlay opacity slider
        opacity_row = Adw.ActionRow(
            title=_("Overlay opacity"),
            subtitle=_("Controls bar background darkness."),
        )
        opacity_row.add_prefix(Gtk.Image.new_from_icon_name("weather-clear-night-symbolic"))
        self._opacity_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 100, 5
        )
        self._opacity_scale.set_value(self._settings.get("overlay-opacity"))
        self._opacity_scale.set_hexpand(True)
        self._opacity_scale.set_valign(Gtk.Align.CENTER)
        self._opacity_scale.set_size_request(180, -1)
        self._opacity_scale.connect("value-changed", self._on_overlay_opacity)
        opacity_row.add_suffix(self._opacity_scale)
        preview.add(opacity_row)

        # Controls opacity slider
        controls_opacity_row = Adw.ActionRow(
            title=_("Controls opacity"),
            subtitle=_("Transparency of the buttons over the preview."),
        )
        controls_opacity_row.add_prefix(Gtk.Image.new_from_icon_name("preferences-desktop-accessibility-symbolic"))
        self._controls_opacity_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 20, 100, 5
        )
        self._controls_opacity_scale.set_value(self._settings.get("controls-opacity"))
        self._controls_opacity_scale.set_hexpand(True)
        self._controls_opacity_scale.set_valign(Gtk.Align.CENTER)
        self._controls_opacity_scale.set_size_request(180, -1)
        self._controls_opacity_scale.connect("value-changed", self._on_controls_opacity)
        controls_opacity_row.add_suffix(self._controls_opacity_scale)
        preview.add(controls_opacity_row)

        self._v4l2_row = Adw.SwitchRow(
            title=_("Direct V4L2 access"),
            subtitle=_("Bypass PipeWire and access the camera directly. May fix flickering on some webcams."),
        )
        self._v4l2_row.add_prefix(Gtk.Image.new_from_icon_name("camera-video-symbolic"))
        self._v4l2_row.set_active(self._settings.get("prefer-v4l2"))
        self._v4l2_row.connect("notify::active", self._on_prefer_v4l2)
        preview.add(self._v4l2_row)

        content.append(preview)

        # -- Camera group (resolution, FPS, timer) ---------------------------
        camera_group = Adw.PreferencesGroup(title=_("Camera"))
        camera_group.set_header_suffix(self._make_group_reset_button(
            _("Reset camera settings"), self._on_reset_camera,
        ))

        # Resolution — fixed tiers, filtered per-camera
        self._res_combo = Adw.ComboRow(title=_("Resolution"))
        self._res_combo.add_prefix(Gtk.Image.new_from_icon_name("video-display-symbolic"))
        self._all_res_tiers: list[tuple[str, str]] = [
            ("", _("Auto")),
            ("480", "480p"),
            ("720", "720p (HD)"),
            ("1080", "1080p (Full HD)"),
            ("2160", "4K (UHD)"),
        ]
        self._res_values: list[str] = [v for v, _lbl in self._all_res_tiers]
        res_model = Gtk.StringList()
        for _v, label in self._all_res_tiers:
            res_model.append(label)
        self._res_combo.set_model(res_model)
        current_res = self._settings.get("preferred-resolution")
        try:
            self._res_combo.set_selected(self._res_values.index(current_res))
        except ValueError:
            self._res_combo.set_selected(0)
        self._res_combo.connect("notify::selected", self._on_resolution)
        camera_group.add(self._res_combo)

        # FPS limit
        self._fps_combo = Adw.ComboRow(title=_("FPS limit"))
        self._fps_combo.add_prefix(Gtk.Image.new_from_icon_name("media-playback-start-symbolic"))
        fps_model = Gtk.StringList()
        for label in (_("Auto"), "15", "24", "30", "60"):
            fps_model.append(label)
        self._fps_combo.set_model(fps_model)
        _FPS_VALUES = [0, 15, 24, 30, 60]
        current_fps = self._settings.get("fps-limit")
        try:
            self._fps_combo.set_selected(_FPS_VALUES.index(current_fps))
        except ValueError:
            self._fps_combo.set_selected(0)
        self._fps_combo.connect("notify::selected", self._on_fps_limit)
        camera_group.add(self._fps_combo)


        # Capture timer
        self._timer_row = Adw.ComboRow(
            title=_("Capture timer"),
            subtitle=_("Countdown before taking a photo."),
        )
        self._timer_row.add_prefix(Gtk.Image.new_from_icon_name("timer-symbolic"))
        timer_model = Gtk.StringList()
        for label in (_("Off"), "3s", "5s", "10s"):
            timer_model.append(label)
        self._timer_row.set_model(timer_model)
        _TIMER_VALUES = [0, 3, 5, 10]
        current_timer = self._settings.get("capture-timer")
        try:
            self._timer_row.set_selected(_TIMER_VALUES.index(current_timer))
        except ValueError:
            self._timer_row.set_selected(0)
        self._timer_row.connect("notify::selected", self._on_capture_timer)
        camera_group.add(self._timer_row)

        content.append(camera_group)

    def _build_recording(self, content: Gtk.Box) -> None:
        """Recording codec/container/bitrate settings."""
        group = Adw.PreferencesGroup(title=_("Recording"))
        group.set_header_suffix(self._make_group_reset_button(
            _("Reset recording settings"), self._on_reset_recording,
        ))

        # Video codec
        vcodec_model = Gtk.StringList.new(["H.264", "H.265", "VP9", "MJPEG"])
        self._vcodec_row = Adw.ComboRow(
            title=_("Video Codec"),
            model=vcodec_model,
        )
        _vcodec_map = {"h264": 0, "h265": 1, "vp9": 2, "mjpeg": 3}
        _vcodec_keys = ["h264", "h265", "vp9", "mjpeg"]
        cur = self._settings.get("recording-video-codec")
        self._vcodec_row.set_selected(_vcodec_map.get(cur, 0))

        def _on_vcodec(row, _pspec):
            idx = row.get_selected()
            self._settings.set("recording-video-codec", _vcodec_keys[idx])
            self.emit("recording-config-changed")

        self._vcodec_row.connect("notify::selected", _on_vcodec)
        group.add(self._vcodec_row)

        # Audio codec
        acodec_model = Gtk.StringList.new(["Opus", "AAC", "MP3", "Vorbis"])
        self._acodec_row = Adw.ComboRow(
            title=_("Audio Codec"),
            model=acodec_model,
        )
        _acodec_map = {"opus": 0, "aac": 1, "mp3": 2, "vorbis": 3}
        _acodec_keys = ["opus", "aac", "mp3", "vorbis"]
        cur_a = self._settings.get("recording-audio-codec")
        self._acodec_row.set_selected(_acodec_map.get(cur_a, 0))

        def _on_acodec(row, _pspec):
            idx = row.get_selected()
            self._settings.set("recording-audio-codec", _acodec_keys[idx])
            self.emit("recording-config-changed")

        self._acodec_row.connect("notify::selected", _on_acodec)
        group.add(self._acodec_row)

        # Container format
        container_model = Gtk.StringList.new(["MKV", "WebM", "MP4"])
        self._container_row = Adw.ComboRow(
            title=_("Container"),
            model=container_model,
        )
        _container_map = {"mkv": 0, "webm": 1, "mp4": 2}
        _container_keys = ["mkv", "webm", "mp4"]
        cur_c = self._settings.get("recording-container")
        self._container_row.set_selected(_container_map.get(cur_c, 0))

        def _on_container(row, _pspec):
            idx = row.get_selected()
            container = _container_keys[idx]
            self._settings.set("recording-container", container)
            # Auto-correct codecs incompatible with the chosen container
            if container == "webm":
                self._vcodec_row.set_selected(_vcodec_map["vp9"])
                acodec = _acodec_keys[self._acodec_row.get_selected()]
                if acodec not in ("opus", "vorbis"):
                    self._acodec_row.set_selected(_acodec_map["opus"])
            elif container == "mp4":
                vcodec = _vcodec_keys[self._vcodec_row.get_selected()]
                if vcodec in ("vp9", "mjpeg"):
                    self._vcodec_row.set_selected(_vcodec_map["h264"])
                if _acodec_keys[self._acodec_row.get_selected()] == "vorbis":
                    self._acodec_row.set_selected(_acodec_map["aac"])
            self.emit("recording-config-changed")

        self._container_row.connect("notify::selected", _on_container)
        group.add(self._container_row)

        # Video bitrate
        bitrate_adj = Gtk.Adjustment(
            value=self._settings.get("recording-video-bitrate"),
            lower=500,
            upper=50000,
            step_increment=500,
            page_increment=2000,
        )
        self._bitrate_row = Adw.SpinRow(
            title=_("Video Bitrate (kbps)"),
            adjustment=bitrate_adj,
        )

        def _on_bitrate(row, _pspec):
            self._settings.set("recording-video-bitrate", int(row.get_value()))
            self.emit("recording-config-changed")

        self._bitrate_row.connect("notify::value", _on_bitrate)
        group.add(self._bitrate_row)

        content.append(group)

    def _build_tools(self, content: Gtk.Box) -> None:
        import threading

        self._threading = threading

        from ui.qr_dialog import parse_qr, QrDialog

        self._parse_qr = parse_qr
        self._QrDialog = QrDialog

        # --- QR Code Scanner ---
        qr_group = Adw.PreferencesGroup(title=_("QR Code Scanner"))
        self._qr_row = Adw.SwitchRow(
            title=_("Scan QR Codes"),
            subtitle=_("Detect QR codes in the camera feed"),
        )
        self._qr_row.add_prefix(Gtk.Image.new_from_icon_name("camera-photo-symbolic"))
        self._qr_row.connect("notify::active", self._on_qr_toggled)
        qr_group.add(self._qr_row)
        content.append(qr_group)

    def _build_virtual_camera(self, content: Gtk.Box) -> None:
        vc_group = Adw.PreferencesGroup(title=_("Virtual Camera"))

        self._vc_status_row = Adw.ActionRow(title=_("Status"))
        self._vc_status_icon = Gtk.Image.new_from_icon_name("emblem-default-symbolic")
        self._vc_status_row.add_prefix(self._vc_status_icon)
        vc_group.add(self._vc_status_row)

        self._vc_device_row = Adw.ActionRow(
            title=_("Device"),
            subtitle=_("Not loaded"),
        )
        self._vc_device_row.add_prefix(Gtk.Image.new_from_icon_name("video-display-symbolic"))
        vc_group.add(self._vc_device_row)

        self._vc_toggle_row = Adw.SwitchRow(
            title=_("Enable virtual camera"),
            subtitle=_("Create a virtual camera output for video calls and streaming."),
        )
        self._vc_toggle_row.add_prefix(Gtk.Image.new_from_icon_name("camera-web-symbolic"))
        self._vc_toggle_row.connect("notify::active", self._on_vc_toggle)
        vc_group.add(self._vc_toggle_row)

        content.append(vc_group)
        self._vc_updating = False
        self._refresh_vc_status()

    # -- handlers ------------------------------------------------------------

    def _on_theme(self, row: Adw.ComboRow, _pspec) -> None:
        idx = row.get_selected()
        value = {0: "light", 1: "dark"}.get(idx, "dark")
        self._settings.set("theme", value)
        style_manager = Adw.StyleManager.get_default()
        scheme_map = {
            "light": Adw.ColorScheme.FORCE_LIGHT,
            "dark": Adw.ColorScheme.FORCE_DARK,
        }
        style_manager.set_color_scheme(scheme_map.get(value, Adw.ColorScheme.FORCE_DARK))

    def _on_mirror(self, row: Adw.SwitchRow, _pspec) -> None:
        active = row.get_active()
        self._settings.set("mirror_preview", active)
        self.emit("mirror-changed", active)

    def _on_prefer_v4l2(self, row: Adw.SwitchRow, _pspec) -> None:
        active = row.get_active()
        self._settings.set("prefer-v4l2", active)
        self.emit("prefer-v4l2-changed", active)

    def _on_show_fps(self, row: Adw.SwitchRow, _pspec) -> None:
        active = row.get_active()
        self._settings.set("show_fps", active)
        self.emit("show-fps-changed", active)


    def _on_hotplug(self, row: Adw.SwitchRow, _pspec) -> None:
        self._settings.set("hotplug_enabled", row.get_active())

    def _on_help_tooltips(self, row: Adw.SwitchRow, _pspec) -> None:
        active = row.get_active()
        self._settings.set("show-help-tooltips", active)
        self.emit("help-tooltips-changed", active)

    def _on_resource_monitor(self, row: Adw.SwitchRow, _pspec) -> None:
        active = row.get_active()
        self._settings.set("resource-monitor-enabled", active)
        self.emit("resource-monitor-changed", active)

    def _on_reset_warnings(self, _btn: Gtk.Button) -> None:
        self._settings.set("resource-warnings-dismissed", [])
        if not self._resource_row.get_active():
            self._resource_row.set_active(True)

    def _on_resolution(self, row: Adw.ComboRow, _pspec) -> None:
        if getattr(self, '_updating_formats', False):
            return
        idx = row.get_selected()
        value = self._res_values[idx] if idx < len(self._res_values) else ""
        self._settings.set("preferred-resolution", value)
        self.emit("resolution-changed", value)

    def update_camera_formats(self, camera) -> None:
        """Filter resolution tiers based on camera's actual max height."""
        max_h = 0
        if hasattr(camera, "formats"):
            for fmt in camera.formats:
                if fmt.height > max_h:
                    max_h = fmt.height

        if max_h == 0:
            return

        self._updating_formats = True
        filtered: list[tuple[str, str]] = []
        for value, label in self._all_res_tiers:
            if not value or int(value) <= max_h:
                filtered.append((value, label))

        self._res_values = [v for v, _lbl in filtered]
        model = Gtk.StringList()
        for _v, label in filtered:
            model.append(label)
        self._res_combo.set_model(model)

        current_res = self._settings.get("preferred-resolution")
        try:
            self._res_combo.set_selected(self._res_values.index(current_res))
        except ValueError:
            self._res_combo.set_selected(0)
        self._updating_formats = False

    def _on_fps_limit(self, row: Adw.ComboRow, _pspec) -> None:
        _FPS_VALUES = [0, 15, 24, 30, 60]
        idx = row.get_selected()
        value = _FPS_VALUES[idx] if idx < len(_FPS_VALUES) else 0
        self._settings.set("fps-limit", value)
        self.emit("fps-limit-changed", value)

    def _on_capture_timer(self, row: Adw.ComboRow, _pspec) -> None:
        if getattr(self, '_syncing_timer', False):
            return
        _TIMER_VALUES = [0, 3, 5, 10]
        idx = row.get_selected()
        value = _TIMER_VALUES[idx] if idx < len(_TIMER_VALUES) else 0
        self._settings.set("capture-timer", value)
        self.emit("capture-timer-changed", value)

    def _on_grid_overlay(self, row: Adw.SwitchRow, _pspec) -> None:
        active = row.get_active()
        self._settings.set("grid_overlay", active)
        self.emit("grid-overlay-changed", active)

    def _on_overlay_opacity(self, scale: Gtk.Scale) -> None:
        value = int(scale.get_value())
        self._settings.set("overlay-opacity", value)
        self.emit("overlay-opacity-changed", value)

    def _on_window_opacity(self, scale: Gtk.Scale) -> None:
        value = int(scale.get_value())
        self._settings.set("window-opacity", value)
        self.emit("window-opacity-changed", value)

    def _on_controls_opacity(self, scale: Gtk.Scale) -> None:
        value = int(scale.get_value())
        self._settings.set("controls-opacity", value)
        self.emit("controls-opacity-changed", value)

    @staticmethod
    def _open_directory(path: str) -> None:
        import subprocess

        os.makedirs(path, exist_ok=True)
        subprocess.Popen(["xdg-open", path])

    # -- Reset buttons -------------------------------------------------------

    def _make_group_reset_button(
        self, tooltip: str, callback
    ) -> Gtk.Button:
        btn = Gtk.Button.new_from_icon_name("edit-undo-symbolic")
        btn.add_css_class("flat")
        btn.set_tooltip_text(_("Reset to defaults"))
        btn.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [tooltip],
        )
        btn.connect("clicked", callback)
        return btn

    def _on_reset_general(self, _btn: Gtk.Button) -> None:
        self._theme_row.set_selected(1)                # dark
        self._hotplug_row.set_active(True)
        self._help_tooltips_row.set_active(True)
        self._resource_row.set_active(True)

    def _on_reset_preview(self, _btn: Gtk.Button) -> None:
        self._mirror_row.set_active(False)
        self._show_fps_row.set_active(True)
        self._grid_row.set_active(False)
        self._window_opacity_scale.set_value(100)
        self._opacity_scale.set_value(75)
        self._controls_opacity_scale.set_value(90)
        self._v4l2_row.set_active(True)

    def _on_reset_camera(self, _btn: Gtk.Button) -> None:
        self._res_combo.set_selected(0)                # Auto
        self._fps_combo.set_selected(0)                # Auto
        self._timer_row.set_selected(0)                # Off

    def _on_reset_recording(self, _btn: Gtk.Button) -> None:
        self._vcodec_row.set_selected(0)               # H.264
        self._acodec_row.set_selected(0)               # Opus
        self._container_row.set_selected(0)            # MKV
        self._bitrate_row.get_adjustment().set_value(8000)

    # -- QR Code handlers ----------------------------------------------------

    def _on_qr_toggled(self, row: Adw.SwitchRow, _pspec) -> None:
        self._qr_active = row.get_active()
        if self._qr_active:
            self._init_qr_detector()
            self._qr_timer_id = GLib.timeout_add(150, self._scan_qr)
        else:
            if self._qr_timer_id:
                GLib.source_remove(self._qr_timer_id)
                self._qr_timer_id = None
            self._last_qr_text = ""
            self._engine.set_overlay_rects([])

    def _init_qr_detector(self) -> None:
        if self._wechat_qr is not None or self._qr_detector is not None:
            return
        try:
            self._wechat_qr = cv2.wechat_qrcode.WeChatQRCode()
        except Exception:
            self._qr_detector = cv2.QRCodeDetector()
        if self._zbar_scanner is None and _HAS_ZBAR:
            try:
                sc = zbar.ImageScanner()
                sc.parse_config("enable")
                sc.set_config(zbar.Symbol.QRCODE, zbar.Config.ENABLE, 0)
                self._zbar_scanner = sc
            except Exception:
                pass

    def _try_detect_qr(self, img):
        if self._wechat_qr is not None:
            results, pts_list = self._wechat_qr.detectAndDecode(img)
            if results and results[0]:
                pts = pts_list[0] if pts_list and len(pts_list) > 0 else None
                return results[0], pts
        elif self._qr_detector is not None:
            data, pts, _straight = self._qr_detector.detectAndDecode(img)
            if data:
                p = pts[0] if pts is not None and pts.ndim == 3 else pts
                return data, p
        # Barcode fallback via zbar
        if self._zbar_scanner is not None:
            try:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
                h, w = gray.shape
                zimg = zbar.Image(w, h, "Y800", gray.tobytes())
                self._zbar_scanner.scan(zimg)
                for sym in zimg:
                    if sym.data:
                        loc = np.array(sym.location, dtype=np.float32)
                        return f"barcode:{sym.data}", loc
            except Exception:
                pass
        return "", None

    def _scan_qr(self) -> bool:
        if not self._qr_active:
            return False
        if self._qr_scanning:
            return True
        frame = self._engine.last_frame_bgr
        if frame is None:
            return True
        self._qr_scanning = True
        import threading

        threading.Thread(
            target=self._scan_qr_worker, args=(frame.copy(),), daemon=True
        ).start()
        return True

    def _scan_qr_worker(self, frame) -> None:
        try:
            data, points = self._try_detect_qr(frame)

            # Upscale small frames for better detection
            if not data:
                h, w = frame.shape[:2]
                if max(h, w) < 1000:
                    upscaled = cv2.resize(frame, (w * 2, h * 2), interpolation=cv2.INTER_CUBIC)
                    data, points = self._try_detect_qr(upscaled)
                    if points is not None:
                        points = points / 2

            if not data:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                eq = cv2.equalizeHist(gray)
                data, points = self._try_detect_qr(eq)

            # CLAHE for better contrast
            if not data:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
                enhanced = clahe.apply(gray)
                data, points = self._try_detect_qr(enhanced)

            if not data:
                sharp_k = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
                data, points = self._try_detect_qr(cv2.filter2D(frame, -1, sharp_k))
            if not data:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                thresh = cv2.adaptiveThreshold(
                    gray,
                    255,
                    cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                    cv2.THRESH_BINARY,
                    51,
                    10,
                )
                data, points = self._try_detect_qr(thresh)

            rects = []
            if points is not None and len(points) >= 4:
                pts_int = np.int32(points)
                x_min = int(pts_int[:, 0].min())
                y_min = int(pts_int[:, 1].min())
                x_max = int(pts_int[:, 0].max())
                y_max = int(pts_int[:, 1].max())
                rects.append((x_min, y_min, x_max - x_min, y_max - y_min))
            GLib.idle_add(self._scan_qr_done, data, rects)
        except Exception:
            GLib.idle_add(self._scan_qr_done, "", [])

    def _scan_qr_done(self, data: str, rects: list) -> bool:
        self._qr_scanning = False
        self._engine.set_overlay_rects(rects)
        if data and data != self._last_qr_text:
            self._last_qr_text = data
            self.emit("qr-detected", data)
            qr_result = self._parse_qr(data)
            dialog = self._QrDialog(qr_result)
            root = self.get_root()
            if root:
                dialog.set_transient_for(root)
            dialog.connect("close-request", self._on_qr_dialog_closed)
            dialog.present()
        return False

    def _on_qr_dialog_closed(self, dialog) -> bool:
        self._last_qr_text = ""
        dialog.destroy()
        return True

    # -- Virtual Camera handlers ---------------------------------------------

    def _refresh_vc_status(self) -> None:
        self._vc_updating = True

        def _query() -> tuple[bool, str | None, bool]:
            available = VirtualCamera.is_available()
            device = VirtualCamera.find_loopback_device() if available else None
            enabled = VirtualCamera.is_enabled() if available else False
            return available, device, enabled

        def _update(result: tuple[bool, str | None, bool]) -> None:
            available, device, enabled = result
            try:
                if not available:
                    self._vc_status_row.set_subtitle(_("v4l2loopback not available"))
                    self._vc_status_icon.set_from_icon_name("dialog-warning-symbolic")
                    self._vc_toggle_row.set_sensitive(False)
                    return
                if enabled and device:
                    self._vc_status_row.set_subtitle(_("Active"))
                    self._vc_status_icon.set_from_icon_name("emblem-ok-symbolic")
                    self._vc_device_row.set_subtitle(device)
                    self._vc_toggle_row.set_active(True)
                elif device:
                    self._vc_status_row.set_subtitle(_("Module loaded"))
                    self._vc_status_icon.set_from_icon_name("emblem-default-symbolic")
                    self._vc_device_row.set_subtitle(device)
                    self._vc_toggle_row.set_active(False)
                else:
                    self._vc_status_row.set_subtitle(_("Module not loaded"))
                    self._vc_status_icon.set_from_icon_name("dialog-information-symbolic")
                    self._vc_device_row.set_subtitle(_("Not loaded"))
                    self._vc_toggle_row.set_active(False)
            finally:
                self._vc_updating = False

        from utils.async_worker import run_async
        run_async(_query, on_success=_update)

    def _on_vc_toggle(self, row: Adw.SwitchRow, _pspec) -> None:
        if self._vc_updating:
            return
        active = row.get_active()
        VirtualCamera.set_enabled(active)
        self.emit("virtual-camera-toggled", active)
        GLib.timeout_add(500, lambda: (self._refresh_vc_status(), False)[-1])

    def set_vc_toggle_active(self, active: bool) -> None:
        self._vc_updating = True
        self._vc_toggle_row.set_active(active)
        self._vc_updating = False
        self._refresh_vc_status()
