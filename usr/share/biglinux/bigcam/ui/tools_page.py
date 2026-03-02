"""Tools page — QR Code scanner and smile-triggered photo capture."""

from __future__ import annotations

import logging
import os
import time as _time
import threading
from typing import Any

log = logging.getLogger(__name__)

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, GLib, GObject

from utils.i18n import _
from ui.qr_dialog import parse_qr, QrDialog

try:
    import cv2
    import numpy as np

    _HAS_CV2 = True
except ImportError:
    _HAS_CV2 = False

_HAARCASCADES = "/usr/share/opencv4/haarcascades"


class ToolsPage(Gtk.ScrolledWindow):
    """Sidebar page with QR Code scanner and smile-triggered capture."""

    __gtype_name__ = "ToolsPage"

    __gsignals__ = {
        "smile-captured": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "qr-detected": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }

    def __init__(self, stream_engine: Any) -> None:
        super().__init__()
        self._engine = stream_engine
        self._qr_active = False
        self._smile_active = False
        self._qr_timer_id: int | None = None
        self._smile_timer_id: int | None = None
        self._smile_cooldown = False
        self._last_qr_text = ""
        self._qr_scanning = False  # prevent overlapping scans

        # OpenCV detectors (lazy init)
        self._qr_detector = None
        self._wechat_qr = None
        self._face_cascade = None
        self._smile_cascade = None

        if not _HAS_CV2:
            self._build_fallback()
            return
        self._build_ui()

    def _build_fallback(self) -> None:
        status = Adw.StatusPage(
            title=_("OpenCV not available"),
            description=_("Install python-opencv to use tools."),
            icon_name="dialog-warning-symbolic",
        )
        self.set_child(status)

    def _build_ui(self) -> None:
        clamp = Adw.Clamp(maximum_size=400, tightening_threshold=300)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        clamp.set_child(box)
        self.set_child(clamp)

        # --- QR Code Scanner ---
        qr_group = Adw.PreferencesGroup(title=_("QR Code Scanner"))
        box.append(qr_group)

        self._qr_row = Adw.SwitchRow(
            title=_("Scan QR Codes"),
            subtitle=_("Detect QR codes in the camera feed"),
        )
        self._qr_row.connect("notify::active", self._on_qr_toggled)
        qr_group.add(self._qr_row)

        # --- Smile Capture ---
        smile_group = Adw.PreferencesGroup(title=_("Smile Capture"))
        box.append(smile_group)

        self._smile_row = Adw.SwitchRow(
            title=_("Capture on Smile"),
            subtitle=_("Automatically take a photo when a smile is detected"),
        )
        self._smile_row.connect("notify::active", self._on_smile_toggled)
        smile_group.add(self._smile_row)

        # Sensitivity slider
        self._sensitivity_row = Adw.ActionRow(title=_("Sensitivity"))
        self._sensitivity_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 10, 50, 5
        )
        self._sensitivity_scale.set_value(25)
        self._sensitivity_scale.set_hexpand(True)
        self._sensitivity_scale.set_valign(Gtk.Align.CENTER)
        self._sensitivity_row.add_suffix(self._sensitivity_scale)
        smile_group.add(self._sensitivity_row)

        # Status label
        self._smile_status = Gtk.Label(
            label="",
            xalign=0.5,
            css_classes=["dim-label"],
        )
        self._smile_status.set_margin_top(4)
        box.append(self._smile_status)

    # --- QR Code ---

    def _on_qr_toggled(self, row: Adw.SwitchRow, _pspec: Any) -> None:
        self._qr_active = row.get_active()
        log.debug(f"QR toggle: active={self._qr_active}")
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
        """Initialize QR detector — prefer WeChatQRCode for better detection."""
        if self._wechat_qr is not None or self._qr_detector is not None:
            return
        try:
            self._wechat_qr = cv2.wechat_qrcode.WeChatQRCode()
            log.debug("Using WeChatQRCode detector")
        except Exception:
            self._qr_detector = cv2.QRCodeDetector()
            log.debug("Using basic QRCodeDetector")

    def _try_detect_qr(self, img):
        """Try QR detection on a single image, return (data, points) or ("", None)."""
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
        frame_copy = frame.copy()
        log.debug(f"QR scan starting, frame shape: {frame_copy.shape}")
        threading.Thread(
            target=self._scan_qr_worker, args=(frame_copy,), daemon=True
        ).start()
        return True

    def _scan_qr_worker(self, frame) -> None:
        """Run QR detection in background thread."""
        try:
            # Try original frame first
            data, points = self._try_detect_qr(frame)
            log.debug(f"QR worker: original result='{data[:30] if data else ''}'")

            # If not found, try enhanced variants for low-quality cameras
            if not data:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                eq = cv2.equalizeHist(gray)
                data, points = self._try_detect_qr(eq)

            if not data:
                sharp_k = np.array([[0, -1, 0], [-1, 5, -1], [0, -1, 0]])
                sharpened = cv2.filter2D(frame, -1, sharp_k)
                data, points = self._try_detect_qr(sharpened)

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

            # Prepare overlay rects
            rects = []
            if points is not None and len(points) >= 4:
                pts_int = np.int32(points)
                x_min = int(pts_int[:, 0].min())
                y_min = int(pts_int[:, 1].min())
                x_max = int(pts_int[:, 0].max())
                y_max = int(pts_int[:, 1].max())
                rects.append((x_min, y_min, x_max - x_min, y_max - y_min))

            # Update UI from main thread
            GLib.idle_add(self._scan_qr_done, data, rects)
        except Exception:
            GLib.idle_add(self._scan_qr_done, "", [])

    def _scan_qr_done(self, data: str, rects: list) -> bool:
        self._qr_scanning = False
        self._engine.set_overlay_rects(rects)
        if rects:
            log.debug(f"QR overlay rects: {rects}")
        if data and data != self._last_qr_text:
            self._last_qr_text = data
            self.emit("qr-detected", data)
            self._show_qr_result(data)
        return False

    def _show_qr_result(self, text: str) -> bool:
        # Parse QR type and open window
        qr_result = parse_qr(text)
        dialog = QrDialog(qr_result)
        root = self.get_root()
        if root:
            dialog.set_transient_for(root)
        dialog.present()
        return False

    # --- Smile Capture ---

    def _on_smile_toggled(self, row: Adw.SwitchRow, _pspec: Any) -> None:
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
            for x, y, fw, fh in faces:
                roi_gray = gray[y : y + fh, x : x + fw]
                # Look for smiles in the lower half of the face
                lower_half = roi_gray[fh // 2 :, :]
                smiles = self._smile_cascade.detectMultiScale(
                    lower_half,
                    scaleFactor=1.7,
                    minNeighbors=sensitivity,
                    minSize=(25, 15),
                )
                if len(smiles) > 0:
                    GLib.idle_add(self._trigger_smile_capture)
                    return True
        except Exception:
            log.debug("Smile detection error", exc_info=True)
        return True

    def _trigger_smile_capture(self) -> bool:
        if self._smile_cooldown:
            return False
        self._smile_cooldown = True
        self._smile_status.set_text(_("Smile detected! Capturing..."))

        # Capture photo
        from utils import xdg

        timestamp = _time.strftime("%Y%m%d_%H%M%S")
        output_dir = xdg.photos_dir()
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, f"bigcam_smile_{timestamp}.png")

        ok = self._engine.capture_snapshot(output_path)
        if ok:
            self._smile_status.set_text(_("Photo saved!"))
            self.emit("smile-captured", output_path)
        else:
            self._smile_status.set_text(_("Capture failed."))

        # Cooldown 3 seconds before next capture
        GLib.timeout_add(3000, self._reset_smile_cooldown)
        return False

    def _reset_smile_cooldown(self) -> bool:
        self._smile_cooldown = False
        if self._smile_active:
            self._smile_status.set_text(_("Watching for smiles..."))
        return False
