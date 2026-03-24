"""Welcome dialog for BigCam."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from utils.i18n import _
from utils.settings_manager import SettingsManager


class WelcomeDialog:
    """Welcome dialog explaining BigCam features."""

    def __init__(self, parent_window: Gtk.Window, settings: SettingsManager) -> None:
        self._parent = parent_window
        self._settings = settings
        self._dialog: Adw.Dialog | None = None
        self._show_switch: Gtk.Switch | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_start(20)
        content.set_margin_end(20)
        content.set_margin_top(20)
        content.set_margin_bottom(12)

        # Header
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        header.set_halign(Gtk.Align.CENTER)

        icon = Gtk.Image.new_from_icon_name("bigcam")
        icon.set_pixel_size(64)
        header.append(icon)

        title = Gtk.Label()
        title.set_markup(
            "<span size='xx-large' weight='bold'>"
            + _("Welcome to BigCam")
            + "</span>"
        )
        header.append(title)

        subtitle = Gtk.Label()
        subtitle.set_markup(
            "<span size='large'>"
            + _("Your universal webcam control center for Linux")
            + "</span>"
        )
        subtitle.add_css_class("dim-label")
        header.append(subtitle)

        content.append(header)

        # Two columns of features
        columns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        columns.set_margin_top(18)
        columns.set_halign(Gtk.Align.CENTER)
        columns.set_hexpand(True)

        left_features = [
            (
                "camera-photo-symbolic",
                _("Photo & Video Capture"),
                _("Take photos and record videos\nwith timer and countdown support"),
            ),
            (
                "object-flip-horizontal-symbolic",
                _("Mirror Preview"),
                _("Flip the camera preview\nhorizontally like a mirror"),
            ),
            (
                "applications-graphics-symbolic",
                _("Real-Time Effects"),
                _("Apply brightness, contrast, blur,\nsepia, vignette and more effects live"),
            ),
            (
                "phone-symbolic",
                _("Phone as Webcam"),
                _("Use your smartphone camera\nwirelessly as a webcam"),
            ),
        ]

        right_features = [
            (
                "camera-switch-symbolic",
                _("Multiple Cameras"),
                _("Switch between USB, IP, and\nvirtual cameras with hotplug support"),
            ),
            (
                "scanner-symbolic",
                _("QR Code Scanner"),
                _("Scan QR codes and barcodes\ndirectly from the camera feed"),
            ),
            (
                "camera-web-symbolic",
                _("Virtual Camera"),
                _("Create a virtual camera device\nfor use in video calls"),
            ),
        ]

        left_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        left_col.set_hexpand(True)
        for icon_name, feat_title, feat_desc in left_features:
            left_col.append(self._create_feature_box(icon_name, feat_title, feat_desc))
        columns.append(left_col)

        right_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        right_col.set_hexpand(True)
        for icon_name, feat_title, feat_desc in right_features:
            right_col.append(self._create_feature_box(icon_name, feat_title, feat_desc))
        columns.append(right_col)

        content.append(columns)

        # Keyboard shortcuts hint
        shortcuts_label = Gtk.Label()
        shortcuts_label.set_markup(
            "<span size='small'>"
            + _("Tip: Press Space to capture, Ctrl+R to record, Tab to toggle sidebar")
            + "</span>"
        )
        shortcuts_label.add_css_class("dim-label")
        shortcuts_label.set_margin_top(12)
        content.append(shortcuts_label)

        scrolled.set_child(content)

        # Fixed bottom bar (outside scrolled area)
        bottom_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        bottom_bar.set_margin_start(20)
        bottom_bar.set_margin_end(20)
        bottom_bar.set_margin_top(12)
        bottom_bar.set_margin_bottom(16)

        # Switch on left
        self._show_switch = Gtk.Switch()
        self._show_switch.set_valign(Gtk.Align.CENTER)
        self._show_switch.set_active(self._settings.get("show-welcome"))

        switch_label = Gtk.Label(label=_("Show dialog on startup"))
        switch_label.set_xalign(0)

        switch_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        switch_box.append(self._show_switch)
        switch_box.append(switch_label)
        switch_box.set_hexpand(True)

        bottom_bar.append(switch_box)

        # Button on right
        start_btn = Gtk.Button(label=_("Let's Start"))
        start_btn.add_css_class("suggested-action")
        start_btn.add_css_class("pill")
        start_btn.set_size_request(150, -1)
        start_btn.connect("clicked", self._on_close)
        bottom_bar.append(start_btn)

        # Main container: scrolled + separator + fixed bottom
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.append(scrolled)
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        outer.append(sep)
        outer.append(bottom_bar)

        self._dialog = Adw.Dialog()
        self._dialog.set_content_width(900)
        self._dialog.set_content_height(650)
        self._dialog.set_child(outer)

    def present(self) -> None:
        if self._dialog and self._parent:
            self._dialog.present(self._parent)

    def _create_feature_box(self, icon_name: str, title: str, description: str) -> Gtk.Box:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(32)
        icon.set_valign(Gtk.Align.START)
        icon.add_css_class("dim-label")
        row.append(icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)

        title_label = Gtk.Label()
        title_label.set_markup(f"<b>{GLib.markup_escape_text(title)}</b>")
        title_label.set_halign(Gtk.Align.START)
        title_label.set_wrap(True)
        text_box.append(title_label)

        desc_label = Gtk.Label(label=description)
        desc_label.set_halign(Gtk.Align.START)
        desc_label.set_wrap(True)
        desc_label.set_xalign(0)
        desc_label.add_css_class("dim-label")
        desc_label.set_max_width_chars(40)
        text_box.append(desc_label)

        row.append(text_box)
        return row

    def _on_close(self, _btn: Gtk.Button) -> None:
        if self._show_switch:
            self._settings.set("show-welcome", self._show_switch.get_active())
        if self._dialog:
            self._dialog.close()

    @staticmethod
    def should_show(settings: SettingsManager) -> bool:
        return bool(settings.get("show-welcome"))
