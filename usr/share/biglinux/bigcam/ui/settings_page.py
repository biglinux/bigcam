"""Settings page – global preferences, tools, and virtual camera."""

from __future__ import annotations

import os

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

_HAARCASCADES = "/usr/share/opencv4/haarcascades"


class SettingsPage(Gtk.ScrolledWindow):
    """Application-wide preferences using Adw.PreferencesGroup widgets."""

    __gsignals__ = {
        "show-fps-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
        "mirror-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
        "smile-captured": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "qr-detected": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "virtual-camera-toggled": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
        "resolution-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "fps-limit-changed": (GObject.SignalFlags.RUN_LAST, None, (int,)),
        "grid-overlay-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
        "resolution-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "fps-limit-changed": (GObject.SignalFlags.RUN_LAST, None, (int,)),
        "grid-overlay-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
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
        self._smile_active = False
        self._qr_timer_id: int | None = None
        self._smile_timer_id: int | None = None
        self._smile_cooldown = False
        self._last_qr_text = ""
        self._qr_scanning = False
        self._qr_detector = None
        self._wechat_qr = None
        self._face_cascade = None
        self._smile_cascade = None

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
        self._build_preview(content)
        if _HAS_CV2 and stream_engine is not None:
            self._build_tools(content)
        self._build_virtual_camera(content)
        self._build_advanced(content)

        clamp.set_child(content)
        self.set_child(clamp)

    def _build_general(self, content: Gtk.Box) -> None:
        general = Adw.PreferencesGroup(title=_("General"))

        # Photo directory
        photo_row = Adw.ActionRow(
            title=_("Photo directory"),
            subtitle=xdg.photos_dir(),
        )
        photo_open_btn = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        photo_open_btn.set_valign(Gtk.Align.CENTER)
        photo_open_btn.set_tooltip_text(_("Open photos folder"))
        photo_open_btn.add_css_class("flat")
        photo_open_btn.connect("clicked",
                               lambda _b: self._open_directory(xdg.photos_dir()))
        photo_row.add_suffix(photo_open_btn)
        photo_row.set_activatable(False)
        general.add(photo_row)

        # Video directory
        video_row = Adw.ActionRow(
            title=_("Video directory"),
            subtitle=xdg.videos_dir(),
        )
        video_open_btn = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        video_open_btn.set_valign(Gtk.Align.CENTER)
        video_open_btn.set_tooltip_text(_("Open videos folder"))
        video_open_btn.add_css_class("flat")
        video_open_btn.connect("clicked",
                               lambda _b: self._open_directory(xdg.videos_dir()))
        video_row.add_suffix(video_open_btn)
        video_row.set_activatable(False)
        general.add(video_row)

        # Theme
        theme_row = Adw.ComboRow(title=_("Theme"))
        theme_model = Gtk.StringList()
        for t in (_("System"), _("Light"), _("Dark")):
            theme_model.append(t)
        theme_row.set_model(theme_model)
        theme_idx = {"system": 0, "light": 1, "dark": 2}.get(
            self._settings.get("theme"), 0
        )
        theme_row.set_selected(theme_idx)
        theme_row.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Application theme")]
        )
        theme_row.connect("notify::selected", self._on_theme)
        general.add(theme_row)

        content.append(general)

    def _build_preview(self, content: Gtk.Box) -> None:
        preview = Adw.PreferencesGroup(title=_("Preview"))

        mirror_row = Adw.SwitchRow(
            title=_("Mirror preview"),
            subtitle=_("Flip the preview horizontally like a mirror."),
        )
        mirror_row.set_active(self._settings.get("mirror_preview"))
        mirror_row.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Mirror preview")]
        )
        mirror_row.connect("notify::active", self._on_mirror)
        preview.add(mirror_row)

        show_fps_row = Adw.SwitchRow(
            title=_("Show FPS counter"),
        )
        show_fps_row.set_active(self._settings.get("show_fps"))
        show_fps_row.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Show FPS counter")]
        )
        show_fps_row.connect("notify::active", self._on_show_fps)
        preview.add(show_fps_row)

        grid_row = Adw.SwitchRow(
            title=_("Grid overlay"),
            subtitle=_("Show a rule-of-thirds grid over the preview."),
        )
        grid_row.set_active(self._settings.get("grid_overlay"))
        grid_row.connect("notify::active", self._on_grid_overlay)
        preview.add(grid_row)

        content.append(preview)

        # -- Camera group (resolution, FPS, timer) ---------------------------
        camera_group = Adw.PreferencesGroup(title=_("Camera"))

        # Resolution
        self._res_combo = Adw.ComboRow(title=_("Resolution"))
        res_model = Gtk.StringList()
        for label in (_("Auto"), "640×480", "1280×720", "1920×1080", "3840×2160"):
            res_model.append(label)
        self._res_combo.set_model(res_model)
        _RES_VALUES = ["", "480", "720", "1080", "2160"]
        current_res = self._settings.get("preferred-resolution")
        try:
            self._res_combo.set_selected(_RES_VALUES.index(current_res))
        except ValueError:
            self._res_combo.set_selected(0)
        self._res_combo.connect("notify::selected", self._on_resolution)
        camera_group.add(self._res_combo)

        # FPS limit
        self._fps_combo = Adw.ComboRow(title=_("FPS limit"))
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
        timer_row = Adw.ComboRow(
            title=_("Capture timer"),
            subtitle=_("Countdown before taking a photo."),
        )
        timer_model = Gtk.StringList()
        for label in (_("Off"), "3s", "5s", "10s"):
            timer_model.append(label)
        timer_row.set_model(timer_model)
        _TIMER_VALUES = [0, 3, 5, 10]
        current_timer = self._settings.get("capture-timer")
        try:
            timer_row.set_selected(_TIMER_VALUES.index(current_timer))
        except ValueError:
            timer_row.set_selected(0)
        timer_row.connect("notify::selected", self._on_capture_timer)
        camera_group.add(timer_row)

        content.append(camera_group)

    def _build_advanced(self, content: Gtk.Box) -> None:
        advanced = Adw.PreferencesGroup(title=_("Advanced"))

        hotplug_row = Adw.SwitchRow(
            title=_("USB hotplug detection"),
            subtitle=_("Automatically detect cameras when plugged or unplugged."),
        )
        hotplug_row.set_active(self._settings.get("hotplug_enabled"))
        hotplug_row.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("USB hotplug detection")]
        )
        hotplug_row.connect("notify::active", self._on_hotplug)
        advanced.add(hotplug_row)

        content.append(advanced)

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
        self._qr_row.connect("notify::active", self._on_qr_toggled)
        qr_group.add(self._qr_row)
        content.append(qr_group)

        # --- Smile Capture ---
        smile_group = Adw.PreferencesGroup(title=_("Smile Capture"))
        self._smile_row = Adw.SwitchRow(
            title=_("Capture on Smile"),
            subtitle=_("Automatically take a photo when a smile is detected"),
        )
        self._smile_row.connect("notify::active", self._on_smile_toggled)
        smile_group.add(self._smile_row)

        self._sensitivity_row = Adw.ActionRow(title=_("Sensitivity"))
        self._sensitivity_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 10, 50, 5
        )
        self._sensitivity_scale.set_value(25)
        self._sensitivity_scale.set_hexpand(True)
        self._sensitivity_scale.set_valign(Gtk.Align.CENTER)
        self._sensitivity_row.add_suffix(self._sensitivity_scale)
        smile_group.add(self._sensitivity_row)

        self._smile_status = Gtk.Label(
            label="", xalign=0.5, css_classes=["dim-label"],
        )
        self._smile_status.set_margin_top(4)
        content.append(smile_group)
        content.append(self._smile_status)

    def _build_virtual_camera(self, content: Gtk.Box) -> None:
        vc_group = Adw.PreferencesGroup(title=_("Virtual Camera"))

        self._vc_status_row = Adw.ActionRow(title=_("Status"))
        self._vc_status_icon = Gtk.Image.new_from_icon_name("emblem-default-symbolic")
        self._vc_status_row.add_prefix(self._vc_status_icon)
        vc_group.add(self._vc_status_row)

        self._vc_device_row = Adw.ActionRow(
            title=_("Device"), subtitle=_("Not loaded"),
        )
        vc_group.add(self._vc_device_row)

        self._vc_toggle_row = Adw.SwitchRow(
            title=_("Enable virtual camera"),
            subtitle=_("Create a virtual camera output for video calls and streaming."),
        )
        self._vc_toggle_row.connect("notify::active", self._on_vc_toggle)
        vc_group.add(self._vc_toggle_row)

        content.append(vc_group)
        self._vc_updating = False
        self._refresh_vc_status()

    # -- handlers ------------------------------------------------------------

    def _on_theme(self, row: Adw.ComboRow, _pspec) -> None:
        idx = row.get_selected()
        value = {0: "system", 1: "light", 2: "dark"}.get(idx, "system")
        self._settings.set("theme", value)
        style_manager = Adw.StyleManager.get_default()
        scheme_map = {
            "system": Adw.ColorScheme.DEFAULT,
            "light": Adw.ColorScheme.FORCE_LIGHT,
            "dark": Adw.ColorScheme.FORCE_DARK,
        }
        style_manager.set_color_scheme(scheme_map.get(value, Adw.ColorScheme.DEFAULT))

    def _on_mirror(self, row: Adw.SwitchRow, _pspec) -> None:
        active = row.get_active()
        self._settings.set("mirror_preview", active)
        self.emit("mirror-changed", active)

    def _on_show_fps(self, row: Adw.SwitchRow, _pspec) -> None:
        active = row.get_active()
        self._settings.set("show_fps", active)
        self.emit("show-fps-changed", active)

    def _on_hotplug(self, row: Adw.SwitchRow, _pspec) -> None:
        self._settings.set("hotplug_enabled", row.get_active())

    def _on_resolution(self, row: Adw.ComboRow, _pspec) -> None:
        _RES_VALUES = ["", "480", "720", "1080", "2160"]
        idx = row.get_selected()
        value = _RES_VALUES[idx] if idx < len(_RES_VALUES) else ""
        self._settings.set("preferred-resolution", value)
        self.emit("resolution-changed", value)

    def _on_fps_limit(self, row: Adw.ComboRow, _pspec) -> None:
        _FPS_VALUES = [0, 15, 24, 30, 60]
        idx = row.get_selected()
        value = _FPS_VALUES[idx] if idx < len(_FPS_VALUES) else 0
        self._settings.set("fps-limit", value)
        self.emit("fps-limit-changed", value)

    def _on_capture_timer(self, row: Adw.ComboRow, _pspec) -> None:
        _TIMER_VALUES = [0, 3, 5, 10]
        idx = row.get_selected()
        value = _TIMER_VALUES[idx] if idx < len(_TIMER_VALUES) else 0
        self._settings.set("capture-timer", value)

    def _on_grid_overlay(self, row: Adw.SwitchRow, _pspec) -> None:
        active = row.get_active()
        self._settings.set("grid_overlay", active)
        self.emit("grid-overlay-changed", active)

    @staticmethod
    def _open_directory(path: str) -> None:
        import subprocess
        os.makedirs(path, exist_ok=True)
        subprocess.Popen(["xdg-open", path])

    # -- QR Code handlers ----------------------------------------------------

    def _on_qr_toggled(self, row: Adw.SwitchRow, _pspec) -> None:
        self._qr_active = row.get_active()
        if self._qr_active:
            self._init_qr_detector()
            self._qr_timer_id = GLib.timeout_add(300, self._scan_qr)
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

    def _try_detect_qr(self, img):
        if self._wechat_qr is not None:
            results, pts_list = self._wechat_qr.detectAndDecode(img)
            if results and results[0]:
                pts = pts_list[0] if pts_list and len(pts_list) > 0 else None
                return results[0], pts
        elif self._qr_detector is not None:
            data, pts, _ = self._qr_detector.detectAndDecode(img)
            if data:
                p = pts[0] if pts is not None and pts.ndim == 3 else pts
                return data, p
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
        threading.Thread(target=self._scan_qr_worker, args=(frame.copy(),), daemon=True).start()
        return True

    def _scan_qr_worker(self, frame) -> None:
        try:
            data, points = self._try_detect_qr(frame)
            if not data:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                eq = cv2.equalizeHist(gray)
                data, points = self._try_detect_qr(eq)
            if not data:
                sharp_k = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
                data, points = self._try_detect_qr(cv2.filter2D(frame, -1, sharp_k))
            if not data:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                thresh = cv2.adaptiveThreshold(
                    gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                    cv2.THRESH_BINARY, 51, 10,
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
            dialog.present()
        return False

    # -- Smile handlers ------------------------------------------------------

    def _on_smile_toggled(self, row: Adw.SwitchRow, _pspec) -> None:
        self._smile_active = row.get_active()
        if self._smile_active:
            if self._face_cascade is None:
                self._face_cascade = cv2.CascadeClassifier(
                    os.path.join(_HAARCASCADES, "haarcascade_frontalface_default.xml")
                )
            if self._smile_cascade is None:
                self._smile_cascade = cv2.CascadeClassifier(
                    os.path.join(_HAARCASCADES, "haarcascade_smile.xml")
                )
            self._smile_cooldown = False
            self._smile_status.set_text(_("Watching for smiles..."))
            self._smile_timer_id = GLib.timeout_add(300, self._detect_smile)
        else:
            if self._smile_timer_id:
                GLib.source_remove(self._smile_timer_id)
                self._smile_timer_id = None
            self._smile_status.set_text("")

    def _detect_smile(self) -> bool:
        if not self._smile_active:
            return False
        if self._smile_cooldown:
            return True
        frame = self._engine.last_frame_bgr
        if frame is None:
            return True
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            faces = self._face_cascade.detectMultiScale(
                gray, scaleFactor=1.3, minNeighbors=5, minSize=(80, 80)
            )
            if len(faces) == 0:
                return True
            sensitivity = int(self._sensitivity_scale.get_value())
            for (x, y, fw, fh) in faces:
                roi_gray = gray[y:y + fh, x:x + fw]
                lower_half = roi_gray[fh // 2:, :]
                smiles = self._smile_cascade.detectMultiScale(
                    lower_half, scaleFactor=1.7, minNeighbors=sensitivity,
                    minSize=(25, 15)
                )
                if len(smiles) > 0:
                    GLib.idle_add(self._trigger_smile_capture)
                    return True
        except Exception:
            pass
        return True

    def _trigger_smile_capture(self) -> bool:
        if self._smile_cooldown:
            return False
        self._smile_cooldown = True
        import time as _time
        ts = _time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(xdg.photos_dir(), f"smile_{ts}.jpg")
        os.makedirs(xdg.photos_dir(), exist_ok=True)
        frame = self._engine.last_frame_bgr
        if frame is not None:
            cv2.imwrite(path, frame)
            self.emit("smile-captured", path)
        GLib.timeout_add(3000, self._reset_smile_cooldown)
        return False

    def _reset_smile_cooldown(self) -> bool:
        self._smile_cooldown = False
        return False

    # -- Virtual Camera handlers ---------------------------------------------

    def _refresh_vc_status(self) -> None:
        self._vc_updating = True
        try:
            if not VirtualCamera.is_available():
                self._vc_status_row.set_subtitle(_("v4l2loopback not available"))
                self._vc_status_icon.set_from_icon_name("dialog-warning-symbolic")
                self._vc_toggle_row.set_sensitive(False)
                return
            device = VirtualCamera.find_loopback_device()
            enabled = VirtualCamera.is_enabled()
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
