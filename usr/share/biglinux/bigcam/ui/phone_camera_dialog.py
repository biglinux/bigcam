"""Phone camera dialog – connect a smartphone as a webcam via WebRTC."""

from __future__ import annotations

import io
import logging
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, GObject, GdkPixbuf

from core.phone_camera import PhoneCameraServer
from utils.i18n import _

log = logging.getLogger(__name__)

try:
    import qrcode

    _HAS_QR = True
except ImportError:
    _HAS_QR = False


class PhoneCameraDialog(Adw.Dialog):
    """Dialog for starting the phone camera WebRTC server and displaying a QR code."""

    __gsignals__ = {
        "phone-connected": (GObject.SignalFlags.RUN_LAST, None, (int, int)),
        "phone-disconnected": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self, server: PhoneCameraServer) -> None:
        super().__init__()
        self.set_title(_("Phone as Webcam"))
        self.set_content_width(400)
        self.set_content_height(520)

        self._server = server
        self._sig_ids: list[int] = []

        page = Adw.ToolbarView()
        header = Adw.HeaderBar()
        page.add_top_bar(header)

        clamp = Adw.Clamp(maximum_size=380, tightening_threshold=300)
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_top=16,
            margin_bottom=24,
            margin_start=16,
            margin_end=16,
        )

        # -- QR code placeholder --
        self._qr_frame = Gtk.Frame()
        self._qr_frame.add_css_class("card")
        self._qr_picture = Gtk.Picture()
        self._qr_picture.set_size_request(220, 220)
        self._qr_picture.set_halign(Gtk.Align.CENTER)
        self._qr_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._qr_picture.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("QR Code for phone camera connection")],
        )
        self._qr_frame.set_child(self._qr_picture)
        content.append(self._qr_frame)

        # -- URL label (copyable) --
        self._url_label = Gtk.Label(
            label=_("Press Start to generate URL"),
            selectable=True,
            wrap=True,
            css_classes=["caption"],
        )
        self._url_label.set_halign(Gtk.Align.CENTER)
        content.append(self._url_label)

        # -- Status row --
        status_group = Adw.PreferencesGroup()

        self._status_row = Adw.ActionRow(title=_("Status"))
        self._status_dot = Gtk.DrawingArea()
        self._status_dot.set_content_width(12)
        self._status_dot.set_content_height(12)
        self._status_dot.set_valign(Gtk.Align.CENTER)
        self._dot_color = (0.6, 0.6, 0.6)  # gray = idle
        self._status_dot.set_draw_func(self._draw_dot)
        self._status_dot.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Phone camera status: Idle")],
        )
        self._status_row.add_prefix(self._status_dot)
        self._status_row.set_subtitle(_("Idle"))
        status_group.add(self._status_row)

        self._res_row = Adw.ActionRow(
            title=_("Resolution"),
            subtitle="—",
        )
        res_icon = Gtk.Image.new_from_icon_name("view-fullscreen-symbolic")
        res_icon.set_valign(Gtk.Align.CENTER)
        self._res_row.add_prefix(res_icon)
        status_group.add(self._res_row)

        content.append(status_group)

        # -- Action buttons --
        btn_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
            halign=Gtk.Align.CENTER,
        )

        self._start_btn = Gtk.Button(label=_("Start"))
        self._start_btn.add_css_class("suggested-action")
        self._start_btn.add_css_class("pill")
        self._start_btn.connect("clicked", self._on_start)
        btn_box.append(self._start_btn)

        self._stop_btn = Gtk.Button(label=_("Stop"))
        self._stop_btn.add_css_class("destructive-action")
        self._stop_btn.add_css_class("pill")
        self._stop_btn.set_visible(False)
        self._stop_btn.connect("clicked", self._on_stop)
        btn_box.append(self._stop_btn)

        content.append(btn_box)

        # -- Instructions --
        instructions = Gtk.Label(
            label=_(
                "1. Press Start\n"
                "2. Scan the QR code with your phone\n"
                "3. Accept the security warning\n"
                "4. Allow camera access and press Start"
            ),
            wrap=True,
            css_classes=["dim-label", "caption"],
        )
        instructions.set_halign(Gtk.Align.START)
        content.append(instructions)

        clamp.set_child(content)
        page.set_content(clamp)
        self.set_child(page)

        # Connect server signals
        self._sig_ids.append(self._server.connect("connected", self._on_connected))
        self._sig_ids.append(
            self._server.connect("disconnected", self._on_disconnected)
        )
        self._sig_ids.append(
            self._server.connect("status-changed", self._on_status_changed)
        )

        # If server is already running, update the UI
        if self._server.running:
            self._show_running_state()

        self.connect("closed", self._on_dialog_closed)

    # -- drawing -------------------------------------------------------------

    def _draw_dot(self, _area: Gtk.DrawingArea, cr: Any, w: int, h: int) -> None:
        r, g, b = self._dot_color
        cr.set_source_rgb(r, g, b)
        cr.arc(w / 2, h / 2, min(w, h) / 2, 0, 6.2832)
        cr.fill()

    # -- handlers ------------------------------------------------------------

    def _on_start(self, _btn: Gtk.Button) -> None:
        if not self._server.available():
            self._show_missing_deps()
            return
        if self._server.start():
            self._show_running_state()

    def _on_stop(self, _btn: Gtk.Button) -> None:
        self._server.stop()
        self._start_btn.set_visible(True)
        self._stop_btn.set_visible(False)
        self._url_label.set_label(_("Press Start to generate URL"))
        self._qr_picture.set_paintable(None)
        self._set_dot_color(0.6, 0.6, 0.6)
        self._status_row.set_subtitle(_("Idle"))
        self._res_row.set_subtitle("—")
        self.emit("phone-disconnected")

    def _on_connected(self, _server: PhoneCameraServer, w: int, h: int) -> None:
        self._set_dot_color(0.2, 0.78, 0.35)  # green
        self._status_row.set_subtitle(_("Connected"))
        self._res_row.set_subtitle(f"{w} × {h}")
        self.emit("phone-connected", w, h)

    def _on_disconnected(self, _server: PhoneCameraServer) -> None:
        self._set_dot_color(0.85, 0.2, 0.2)  # red
        self._status_row.set_subtitle(_("Disconnected"))
        self._res_row.set_subtitle("—")
        self.emit("phone-disconnected")

    def _on_status_changed(self, _server: PhoneCameraServer, status: str) -> None:
        if status == "listening":
            self._set_dot_color(1.0, 0.76, 0.03)  # yellow
            self._status_row.set_subtitle(_("Waiting for connection..."))
            self._status_dot.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [_("Phone camera status: Waiting for connection")],
            )
        elif status == "connected":
            self._set_dot_color(0.2, 0.78, 0.35)
            self._status_row.set_subtitle(_("Connected"))
            self._status_dot.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [_("Phone camera status: Connected")],
            )
        elif status in ("disconnected", "stopped"):
            self._set_dot_color(0.6, 0.6, 0.6)
            self._status_row.set_subtitle(_("Idle"))
            self._status_dot.update_property(
                [Gtk.AccessibleProperty.LABEL],
                [_("Phone camera status: Idle")],
            )

    def _on_dialog_closed(self, _dialog: Adw.Dialog) -> None:
        for sid in self._sig_ids:
            self._server.disconnect(sid)
        self._sig_ids.clear()

    # -- helpers -------------------------------------------------------------

    def _show_running_state(self) -> None:
        url = self._server.get_url()
        self._url_label.set_label(url)
        self._start_btn.set_visible(False)
        self._stop_btn.set_visible(True)
        self._set_dot_color(1.0, 0.76, 0.03)  # yellow = waiting
        self._status_row.set_subtitle(_("Waiting for connection..."))
        self._generate_qr(url)

    def _generate_qr(self, url: str) -> None:
        if not _HAS_QR:
            log.warning("python-qrcode not installed, cannot generate QR code")
            self._url_label.set_label(
                url + "\n" + _("(install python-qrcode for QR code)")
            )
            return

        qr_img = qrcode.make(url, border=2)
        buf = io.BytesIO()
        qr_img.save(buf, format="PNG")
        buf.seek(0)

        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.write(buf.read())
        loader.close()
        pixbuf = loader.get_pixbuf()

        if pixbuf:
            texture = self._pixbuf_to_texture(pixbuf)
            self._qr_picture.set_paintable(texture)

    @staticmethod
    def _pixbuf_to_texture(pixbuf: GdkPixbuf.Pixbuf) -> Any:
        from gi.repository import Gdk

        return Gdk.Texture.new_for_pixbuf(pixbuf)

    def _set_dot_color(self, r: float, g: float, b: float) -> None:
        self._dot_color = (r, g, b)
        self._status_dot.queue_draw()

    def _show_missing_deps(self) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Missing Dependencies"),
            body=_(
                "The phone camera feature requires:\n\n"
                "• python-aiohttp\n\n"
                "Install with: sudo pacman -S python-aiohttp"
            ),
        )
        dialog.add_response("ok", _("OK"))
        dialog.set_default_response("ok")
        dialog.present(self)
