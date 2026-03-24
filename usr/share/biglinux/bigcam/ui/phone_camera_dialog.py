"""Phone camera dialog – connect a smartphone as a webcam via Wi-Fi or USB."""

from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk, GObject

from core.phone_camera import PhoneCameraServer
from core.scrcpy_camera import ScrcpyCamera
from core.airplay_receiver import AirPlayReceiver
from core.virtual_camera import VirtualCamera
from utils.i18n import _

log = logging.getLogger(__name__)

# ── Tab identifiers ──────────────────────────────────────────────────
_TAB_USB = "usb"
_TAB_WIFI_ADV = "wifi-adv"
_TAB_AIRPLAY = "airplay"
_TAB_BROWSER = "browser"


class PhoneCameraDialog(Adw.Dialog):
    """Redesigned dialog for phone-as-webcam with modern Adwaita UX."""

    __gsignals__ = {
        "phone-connected": (GObject.SignalFlags.RUN_LAST, None, (int, int)),
        "phone-disconnected": (GObject.SignalFlags.RUN_LAST, None, ()),
        "scrcpy-connected": (GObject.SignalFlags.RUN_LAST, None, (int, int)),
        "scrcpy-disconnected": (GObject.SignalFlags.RUN_LAST, None, ()),
        "scrcpy-prepare": (GObject.SignalFlags.RUN_LAST, None, ()),
        "airplay-connected": (GObject.SignalFlags.RUN_LAST, None, (int, int)),
        "airplay-disconnected": (GObject.SignalFlags.RUN_LAST, None, ()),
        "airplay-prepare": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(
        self,
        server: PhoneCameraServer,
        scrcpy: ScrcpyCamera | None = None,
        airplay: AirPlayReceiver | None = None,
    ) -> None:
        super().__init__()
        self.set_title(_("Phone as Webcam"))
        self.set_content_width(660)
        self.set_content_height(640)
        self.add_css_class("phone-camera-dialog")

        self._server = server
        self._scrcpy = scrcpy or ScrcpyCamera()
        self._airplay = airplay or AirPlayReceiver()
        self._server_sig_ids: list[int] = []
        self._scrcpy_sig_ids: list[int] = []
        self._airplay_sig_ids: list[int] = []
        self._usb_poll_id: int = 0

        # Active mode tracking (None = idle, or tab id)
        self._active_mode: str | None = None
        # Track which tab started scrcpy (USB or Wi-Fi)
        self._scrcpy_tab: str | None = None

        self._build_ui()
        self._connect_backend_signals()
        self.connect("closed", self._on_dialog_closed)
        self._restore_running_state()

    # ══════════════════════════════════════════════════════════════════
    #  UI BUILD
    # ══════════════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()

        # ── Header bar with ViewSwitcher ─────────────────────────────
        header = Adw.HeaderBar()
        self._view_switcher = Adw.ViewSwitcher()
        self._view_switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(self._view_switcher)

        # Info button in header
        info_btn = Gtk.Button(icon_name="help-about-symbolic")
        info_btn.add_css_class("flat")
        info_btn.set_tooltip_text(_("About connection methods"))
        info_btn.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Information about connection methods")],
        )
        info_btn.connect("clicked", self._on_info_clicked)
        header.pack_start(info_btn)

        toolbar_view.add_top_bar(header)

        # ── ViewStack (tabs) ─────────────────────────────────────────
        self._stack = Adw.ViewStack()

        # Tab 1: Browser (Wi-Fi universal)
        browser_page = self._stack.add_titled_with_icon(
            self._build_browser_page(),
            _TAB_BROWSER,
            _("Browser"),
            "web-browser-symbolic",
        )

        # Tab 2: Wi-Fi (ADB wireless)
        wifi_adv_page = self._stack.add_titled_with_icon(
            self._build_wifi_adv_page(),
            _TAB_WIFI_ADV,
            _("Wi-Fi"),
            "network-wireless-symbolic",
        )

        # Tab 3: USB
        usb_page = self._stack.add_titled_with_icon(
            self._build_usb_page(),
            _TAB_USB,
            _("USB"),
            "phone-symbolic",
        )

        # Tab 4: AirPlay
        airplay_page = self._stack.add_titled_with_icon(
            self._build_airplay_page(),
            _TAB_AIRPLAY,
            _("AirPlay"),
            "phone-apple-iphone-symbolic",
        )

        self._view_switcher.set_stack(self._stack)

        # Store page refs for badge manipulation
        self._tab_pages = {
            _TAB_USB: usb_page,
            _TAB_WIFI_ADV: wifi_adv_page,
            _TAB_AIRPLAY: airplay_page,
            _TAB_BROWSER: browser_page,
        }

        toolbar_view.set_content(self._stack)

        # ── Footer status bar ────────────────────────────────────────
        footer = self._build_footer()
        toolbar_view.add_bottom_bar(footer)

        self.set_child(toolbar_view)

    # ── Footer (status + resolution) ─────────────────────────────────

    def _build_footer(self) -> Gtk.Widget:
        footer = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
            margin_start=16,
            margin_end=16,
            margin_top=8,
            margin_bottom=8,
        )
        footer.add_css_class("phone-dialog-footer")

        # Status dot + label (left)
        status_box = Gtk.Box(spacing=6, hexpand=True)
        self._status_dot = Gtk.DrawingArea()
        self._status_dot.set_content_width(10)
        self._status_dot.set_content_height(10)
        self._status_dot.set_valign(Gtk.Align.CENTER)
        self._dot_color = (0.6, 0.6, 0.6)
        self._status_dot.set_draw_func(self._draw_dot)
        status_box.append(self._status_dot)

        self._status_label = Gtk.Label(label=_("Idle"))
        self._status_label.set_halign(Gtk.Align.START)
        self._status_label.add_css_class("caption")
        self._status_label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        status_box.append(self._status_label)
        footer.append(status_box)

        # Resolution (right)
        res_box = Gtk.Box(spacing=4)
        res_icon = Gtk.Image.new_from_icon_name("view-fullscreen-symbolic")
        res_icon.add_css_class("dim-label")
        res_box.append(res_icon)

        self._res_label = Gtk.Label(label="—")
        self._res_label.add_css_class("caption")
        self._res_label.add_css_class("dim-label")
        res_box.append(self._res_label)
        footer.append(res_box)

        return footer

    # ══════════════════════════════════════════════════════════════════
    #  TAB 1: ANDROID USB
    # ══════════════════════════════════════════════════════════════════

    def _build_usb_page(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow(
            vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        clamp = Adw.Clamp(maximum_size=460, tightening_threshold=360)

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_top=20,
            margin_bottom=24,
            margin_start=16,
            margin_end=16,
        )

        # ── Availability check ───────────────────────────────────────
        scrcpy_ok = shutil.which("scrcpy") is not None
        adb_ok = shutil.which("adb") is not None
        v4l2_ok = any(os.path.exists(f"/dev/video{n}") for n in (10, 11, 12, 13))
        self._usb_all_ok = scrcpy_ok and adb_ok and v4l2_ok

        if not self._usb_all_ok:
            content.append(self._make_missing_banner(scrcpy_ok, adb_ok, v4l2_ok))

        # ── Device selector ──────────────────────────────────────────
        dev_group = Adw.PreferencesGroup()

        self._usb_device_row = Adw.ComboRow(title=_("Device"))
        self._usb_device_row.set_model(Gtk.StringList.new([_("Searching…")]))
        dev_icon = Gtk.Image.new_from_icon_name("phone-symbolic")
        dev_icon.set_valign(Gtk.Align.CENTER)
        self._usb_device_row.add_prefix(dev_icon)
        self._usb_only_devices: list = []

        usb_refresh_btn = Gtk.Button(
            icon_name="view-refresh-symbolic",
            tooltip_text=_("Refresh"),
            css_classes=["flat"],
            valign=Gtk.Align.CENTER,
        )
        usb_refresh_btn.connect("clicked", self._on_refresh_usb_devices)
        self._usb_device_row.add_suffix(usb_refresh_btn)
        dev_group.add(self._usb_device_row)
        content.append(dev_group)

        # ── Camera options ───────────────────────────────────────────
        cam_group = Adw.PreferencesGroup()

        self._usb_facing_row = Adw.ComboRow(title=_("Lens"))
        self._usb_facing_row.set_model(
            Gtk.StringList.new([_("Rear camera"), _("Front camera")])
        )
        cam_icon = Gtk.Image.new_from_icon_name("camera-video-symbolic")
        cam_icon.set_valign(Gtk.Align.CENTER)
        self._usb_facing_row.add_prefix(cam_icon)
        cam_group.add(self._usb_facing_row)

        self._usb_resolution_row = Adw.ComboRow(title=_("Quality"))
        self._usb_resolution_row.set_model(
            Gtk.StringList.new(["720p", "1080p", "1440p", "4K", _("Maximum")])
        )
        self._usb_resolution_row.set_selected(1)
        cam_group.add(self._usb_resolution_row)
        content.append(cam_group)

        # ── Advanced (collapsed) ─────────────────────────────────────
        adv_group = Adw.PreferencesGroup()
        self._usb_adv_expander = Adw.ExpanderRow(
            title=_("Advanced"),
            show_enable_switch=False,
        )
        adv_icon = Gtk.Image.new_from_icon_name("emblem-system-symbolic")
        adv_icon.set_valign(Gtk.Align.CENTER)
        self._usb_adv_expander.add_prefix(adv_icon)

        self._usb_fps_row = Adw.ComboRow(title=_("FPS"))
        self._usb_fps_row.set_model(Gtk.StringList.new(["30", "60"]))
        self._usb_adv_expander.add_row(self._usb_fps_row)

        self._usb_bitrate_row = Adw.ComboRow(title=_("Bitrate"))
        self._usb_bitrate_row.set_model(
            Gtk.StringList.new(["8 Mbps", "16 Mbps", "32 Mbps"])
        )
        self._usb_bitrate_row.set_selected(1)
        self._usb_adv_expander.add_row(self._usb_bitrate_row)

        adv_group.add(self._usb_adv_expander)
        content.append(adv_group)

        # ── Spacer + Start button ────────────────────────────────────
        content.append(Gtk.Box(vexpand=True))  # push button down
        content.append(self._build_start_stop_pair(
            start_label=_("Start"),
            stop_label=_("Stop"),
            start_cb=self._on_usb_start,
            stop_cb=self._on_usb_stop,
            ref_prefix="_usb",
        ))

        # Initial device scan
        GLib.idle_add(self._on_refresh_usb_devices, None)

        clamp.set_child(content)
        scroll.set_child(clamp)
        return scroll

    # ══════════════════════════════════════════════════════════════════
    #  TAB 2: ANDROID WI-FI (ADB)
    # ══════════════════════════════════════════════════════════════════

    def _build_wifi_adv_page(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow(
            vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        clamp = Adw.Clamp(maximum_size=460, tightening_threshold=360)

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_top=20,
            margin_bottom=24,
            margin_start=16,
            margin_end=16,
        )

        # ── Availability check ───────────────────────────────────────
        scrcpy_ok = shutil.which("scrcpy") is not None
        adb_ok = shutil.which("adb") is not None
        v4l2_ok = any(os.path.exists(f"/dev/video{n}") for n in (10, 11, 12, 13))
        self._wadv_all_ok = scrcpy_ok and adb_ok and v4l2_ok

        if not self._wadv_all_ok:
            content.append(self._make_missing_banner(scrcpy_ok, adb_ok, v4l2_ok))

        # ── Device selector ──────────────────────────────────────────
        dev_group = Adw.PreferencesGroup()

        self._device_row = Adw.ComboRow(title=_("Device"))
        self._device_row.set_model(Gtk.StringList.new([_("Searching…")]))
        dev_icon = Gtk.Image.new_from_icon_name("phone-symbolic")
        dev_icon.set_valign(Gtk.Align.CENTER)
        self._device_row.add_prefix(dev_icon)
        self._devices: list = []

        suffix_box = Gtk.Box(spacing=4)
        self._adb_wifi_btn = Gtk.Button(
            icon_name="network-wireless-symbolic",
            tooltip_text=_("Switch device to Wi-Fi mode"),
            css_classes=["flat"],
            valign=Gtk.Align.CENTER,
        )
        self._adb_wifi_btn.connect("clicked", self._on_switch_to_wifi)
        self._adb_wifi_btn.set_visible(False)
        suffix_box.append(self._adb_wifi_btn)

        refresh_btn = Gtk.Button(
            icon_name="view-refresh-symbolic",
            tooltip_text=_("Refresh"),
            css_classes=["flat"],
            valign=Gtk.Align.CENTER,
        )
        refresh_btn.connect("clicked", self._on_refresh_devices)
        suffix_box.append(refresh_btn)
        self._device_row.add_suffix(suffix_box)
        dev_group.add(self._device_row)

        # Scan for paired wireless devices
        self._scan_row = Adw.ActionRow(
            title=_("Find wireless devices"),
            subtitle=_("Detect paired devices on your network"),
        )
        scan_icon = Gtk.Image.new_from_icon_name(
            "network-wireless-signal-excellent-symbolic"
        )
        scan_icon.set_valign(Gtk.Align.CENTER)
        self._scan_row.add_prefix(scan_icon)
        self._scan_btn = Gtk.Button(
            label=_("Scan"),
            css_classes=["flat"],
            valign=Gtk.Align.CENTER,
        )
        self._scan_btn.connect("clicked", self._on_scan_and_connect)
        self._scan_row.add_suffix(self._scan_btn)
        self._scan_row.set_activatable_widget(self._scan_btn)
        dev_group.add(self._scan_row)
        content.append(dev_group)

        self._discovered_devices: list[tuple[str, int, str]] = []

        # ── Pairing (expandable) ─────────────────────────────────────
        pair_group = Adw.PreferencesGroup()
        self._pair_expander = Adw.ExpanderRow(
            title=_("Pair new device"),
            subtitle=_("Only needed once per device"),
            show_enable_switch=False,
        )
        pair_icon = Gtk.Image.new_from_icon_name("dialog-password-symbolic")
        pair_icon.set_valign(Gtk.Align.CENTER)
        self._pair_expander.add_prefix(pair_icon)

        # Manual IP:Port + code (primary method, always works)
        self._pair_ip_row = Adw.EntryRow(
            title=_("IP:Port (shown on phone screen)"),
        )
        self._pair_ip_row.set_input_purpose(Gtk.InputPurpose.FREE_FORM)
        self._pair_expander.add_row(self._pair_ip_row)

        self._pair_code_row = Adw.EntryRow(
            title=_("6-digit code (shown on phone screen)"),
        )
        self._pair_code_row.set_input_purpose(Gtk.InputPurpose.NUMBER)
        self._pair_expander.add_row(self._pair_code_row)

        pair_action_row = Adw.ActionRow()
        self._pair_btn = Gtk.Button(label=_("Pair"))
        self._pair_btn.add_css_class("suggested-action")
        self._pair_btn.add_css_class("pill")
        self._pair_btn.set_halign(Gtk.Align.CENTER)
        self._pair_btn.set_valign(Gtk.Align.CENTER)
        self._pair_btn.connect("clicked", self._on_pair_wifi)
        pair_action_row.set_child(self._pair_btn)
        self._pair_expander.add_row(pair_action_row)

        # Auto-discover (secondary — only works with code mode)
        self._discover_row = Adw.ActionRow(
            title=_("Auto-fill IP:Port"),
            subtitle=_(
                "Detects the address automatically. "
                "Requires 'Pair with code' (not QR) open on the phone."
            ),
        )
        self._discover_pair_btn = Gtk.Button(
            label=_("Find"),
            css_classes=["flat"],
            valign=Gtk.Align.CENTER,
        )
        self._discover_pair_btn.connect("clicked", self._on_discover_pairing)
        self._discover_row.add_suffix(self._discover_pair_btn)
        self._discover_row.set_activatable_widget(self._discover_pair_btn)
        self._pair_expander.add_row(self._discover_row)

        pair_group.add(self._pair_expander)
        content.append(pair_group)

        # ── Advanced (collapsed) ─────────────────────────────────────
        adv_group = Adw.PreferencesGroup()
        wadv_adv_expander = Adw.ExpanderRow(
            title=_("Advanced"),
            show_enable_switch=False,
        )
        adv_icon = Gtk.Image.new_from_icon_name("emblem-system-symbolic")
        adv_icon.set_valign(Gtk.Align.CENTER)
        wadv_adv_expander.add_prefix(adv_icon)

        self._facing_row = Adw.ComboRow(title=_("Lens"))
        self._facing_row.set_model(
            Gtk.StringList.new([_("Rear camera"), _("Front camera")])
        )
        wadv_adv_expander.add_row(self._facing_row)

        self._resolution_row = Adw.ComboRow(title=_("Quality"))
        self._resolution_row.set_model(
            Gtk.StringList.new(["720p", "1080p", "1440p", "4K", _("Maximum")])
        )
        self._resolution_row.set_selected(1)
        wadv_adv_expander.add_row(self._resolution_row)

        self._fps_row = Adw.ComboRow(title=_("FPS"))
        self._fps_row.set_model(Gtk.StringList.new(["30", "60"]))
        wadv_adv_expander.add_row(self._fps_row)

        self._bitrate_row = Adw.ComboRow(title=_("Bitrate"))
        self._bitrate_row.set_model(
            Gtk.StringList.new(["8 Mbps", "16 Mbps", "32 Mbps"])
        )
        self._bitrate_row.set_selected(1)
        wadv_adv_expander.add_row(self._bitrate_row)

        adv_group.add(wadv_adv_expander)
        content.append(adv_group)

        # ── Spacer + Start ───────────────────────────────────────────
        content.append(Gtk.Box(vexpand=True))
        content.append(self._build_start_stop_pair(
            start_label=_("Start"),
            stop_label=_("Stop"),
            start_cb=self._on_scrcpy_start,
            stop_cb=self._on_scrcpy_stop,
            ref_prefix="_scrcpy",
        ))

        # Trigger initial device refresh
        GLib.idle_add(self._on_refresh_devices, None)

        clamp.set_child(content)
        scroll.set_child(clamp)
        return scroll

    # ══════════════════════════════════════════════════════════════════
    #  TAB 3: APPLE AIRPLAY
    # ══════════════════════════════════════════════════════════════════

    def _build_airplay_page(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow(
            vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        clamp = Adw.Clamp(maximum_size=460, tightening_threshold=360)

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_top=20,
            margin_bottom=24,
            margin_start=16,
            margin_end=16,
        )

        # ── Availability check ───────────────────────────────────────
        uxplay_ok = AirPlayReceiver.is_available()
        v4l2_ok = any(os.path.exists(f"/dev/video{n}") for n in (10, 11, 12, 13))
        self._airplay_all_ok = uxplay_ok and v4l2_ok

        if not self._airplay_all_ok:
            missing: list[str] = []
            if not uxplay_ok:
                missing.append("uxplay")
            if not v4l2_ok:
                missing.append("v4l2loopback")
            content.append(self._make_missing_banner_raw(missing))

        # ── Server name ──────────────────────────────────────────────
        name_group = Adw.PreferencesGroup(
            description=_(
                "AirPlay mirrors the iPhone screen. "
                "Open the Camera app on your iPhone after connecting "
                "to use it as a webcam."
            ),
        )

        self._airplay_name_row = Adw.EntryRow(title=_("Visible name"))
        self._airplay_name_row.set_text("BigCam")
        name_icon = Gtk.Image.new_from_icon_name("network-server-symbolic")
        name_icon.set_valign(Gtk.Align.CENTER)
        self._airplay_name_row.add_prefix(name_icon)
        name_group.add(self._airplay_name_row)
        content.append(name_group)

        # ── Quality ──────────────────────────────────────────────────
        quality_group = Adw.PreferencesGroup()

        self._airplay_res_row = Adw.ComboRow(title=_("Quality"))
        self._airplay_res_row.set_model(
            Gtk.StringList.new(["720p", "1080p", "1440p"])
        )
        self._airplay_res_row.set_selected(1)
        quality_group.add(self._airplay_res_row)

        self._airplay_fps_row = Adw.ComboRow(title=_("FPS"))
        self._airplay_fps_row.set_model(Gtk.StringList.new(["30", "60"]))
        quality_group.add(self._airplay_fps_row)

        self._airplay_rotate_row = Adw.ComboRow(title=_("Rotation"))
        self._airplay_rotate_row.set_model(
            Gtk.StringList.new([
                _("None"),
                _("90° Right"),
                _("90° Left"),
                _("180°"),
            ])
        )
        quality_group.add(self._airplay_rotate_row)

        content.append(quality_group)

        # ── Spacer + Start ───────────────────────────────────────────
        content.append(Gtk.Box(vexpand=True))
        content.append(self._build_start_stop_pair(
            start_label=_("Start"),
            stop_label=_("Stop"),
            start_cb=self._on_airplay_start,
            stop_cb=self._on_airplay_stop,
            ref_prefix="_airplay",
        ))

        clamp.set_child(content)
        scroll.set_child(clamp)
        return scroll

    # ══════════════════════════════════════════════════════════════════
    #  TAB 4: BROWSER (Wi-Fi universal)
    # ══════════════════════════════════════════════════════════════════

    def _build_browser_page(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow(
            vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        clamp = Adw.Clamp(maximum_size=460, tightening_threshold=360)

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_top=20,
            margin_bottom=24,
            margin_start=16,
            margin_end=16,
        )

        # ── Availability check ───────────────────────────────────────
        self._wifi_available = PhoneCameraServer.available()
        if not self._wifi_available:
            content.append(
                self._make_missing_banner_raw(["python-aiohttp"])
            )

        # ── QR Code area ─────────────────────────────────────────────
        qr_group = Adw.PreferencesGroup(
            description=_(
                "Open this URL in any phone browser to stream the camera"
            ),
        )

        self._qr_picture = Gtk.Picture()
        self._qr_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._qr_picture.set_size_request(180, 180)
        self._qr_picture.set_halign(Gtk.Align.CENTER)
        self._qr_picture.set_margin_top(8)
        self._qr_picture.set_margin_bottom(4)
        self._qr_picture.add_css_class("qr-container")
        self._qr_picture.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("QR Code — scan with your phone")],
        )
        qr_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            halign=Gtk.Align.CENTER,
        )
        qr_box.append(self._qr_picture)
        qr_group.add(qr_box)

        # URL row
        self._wifi_url_row = Adw.ActionRow(
            title=_("URL"),
            subtitle=_("Start to see the address"),
        )
        wifi_icon = Gtk.Image.new_from_icon_name("web-browser-symbolic")
        wifi_icon.set_valign(Gtk.Align.CENTER)
        self._wifi_url_row.add_prefix(wifi_icon)
        self._wifi_copy_btn = Gtk.Button(
            icon_name="edit-copy-symbolic",
            tooltip_text=_("Copy URL"),
            css_classes=["flat"],
            valign=Gtk.Align.CENTER,
        )
        self._wifi_copy_btn.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Copy URL to clipboard")],
        )
        self._wifi_copy_btn.connect("clicked", self._on_copy_wifi_url)
        self._wifi_copy_btn.set_sensitive(False)
        self._wifi_url_row.add_suffix(self._wifi_copy_btn)
        qr_group.add(self._wifi_url_row)
        content.append(qr_group)

        # ── Port setting ─────────────────────────────────────────────
        port_group = Adw.PreferencesGroup()
        self._port_row = Adw.SpinRow.new_with_range(1024, 65535, 1)
        self._port_row.set_title(_("Port"))
        self._port_row.set_value(8443)
        port_group.add(self._port_row)
        content.append(port_group)

        # ── Spacer + Start ───────────────────────────────────────────
        content.append(Gtk.Box(vexpand=True))
        content.append(self._build_start_stop_pair(
            start_label=_("Start"),
            stop_label=_("Stop"),
            start_cb=self._on_wifi_start,
            stop_cb=self._on_wifi_stop,
            ref_prefix="_wifi",
        ))

        clamp.set_child(content)
        scroll.set_child(clamp)
        return scroll

    # ══════════════════════════════════════════════════════════════════
    #  SHARED UI BUILDERS
    # ══════════════════════════════════════════════════════════════════

    def _build_start_stop_pair(
        self,
        start_label: str,
        stop_label: str,
        start_cb: Any,
        stop_cb: Any,
        ref_prefix: str,
    ) -> Gtk.Widget:
        """Build a centered Start / Stop button pair.

        Stores refs as self.{ref_prefix}_start_btn / self.{ref_prefix}_stop_btn.
        """
        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
            halign=Gtk.Align.CENTER,
            margin_top=4,
            margin_bottom=4,
        )

        start_btn = Gtk.Button(label=start_label)
        start_btn.add_css_class("suggested-action")
        start_btn.add_css_class("pill")
        start_btn.set_size_request(140, -1)
        start_btn.connect("clicked", start_cb)
        box.append(start_btn)

        stop_btn = Gtk.Button(label=stop_label)
        stop_btn.add_css_class("destructive-action")
        stop_btn.add_css_class("pill")
        stop_btn.set_size_request(140, -1)
        stop_btn.set_visible(False)
        stop_btn.connect("clicked", stop_cb)
        box.append(stop_btn)

        setattr(self, f"{ref_prefix}_start_btn", start_btn)
        setattr(self, f"{ref_prefix}_stop_btn", stop_btn)

        return box

    @staticmethod
    def _make_missing_banner(
        scrcpy_ok: bool, adb_ok: bool, v4l2_ok: bool
    ) -> Gtk.Widget:
        missing: list[str] = []
        if not scrcpy_ok:
            missing.append("scrcpy")
        if not adb_ok:
            missing.append("android-tools")
        if not v4l2_ok:
            missing.append("v4l2loopback")
        return PhoneCameraDialog._make_missing_banner_raw(missing)

    @staticmethod
    def _make_missing_banner_raw(missing: list[str]) -> Gtk.Widget:
        banner = Adw.Banner()
        banner.set_title(
            _("Install required: %s") % ", ".join(missing)
        )
        banner.set_revealed(True)
        banner.set_button_label("")
        return banner

    @staticmethod
    def _make_step_number(text: str) -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.add_css_class("step-number")
        label.set_valign(Gtk.Align.CENTER)
        label.set_halign(Gtk.Align.CENTER)
        label.set_size_request(28, 28)
        return label

    # ══════════════════════════════════════════════════════════════════
    #  INFO DIALOG
    # ══════════════════════════════════════════════════════════════════

    def _on_info_clicked(self, _btn: Gtk.Button) -> None:
        dialog = Adw.AlertDialog()
        dialog.set_heading(_("How to connect your phone"))
        dialog.set_body(
            "━━━ USB (Android) ━━━\n"
            + _("The easiest and fastest method.") + "\n"
            + _("1. On your Android phone, go to Settings → About Phone") + "\n"
            + _("2. Tap 'Build Number' 7 times to unlock Developer Options") + "\n"
            + _("3. Go to Settings → Developer Options → enable 'USB Debugging'") + "\n"
            + _("4. Connect the USB cable to the computer") + "\n"
            + _("5. Accept the USB Debugging prompt on the phone screen") + "\n"
            + _("6. Click 'Start' on the USB tab") + "\n\n"
            + "━━━ Wi-Fi (Android 11+) ━━━\n"
            + _("No cable needed after the first setup.") + "\n\n"
            + _("EASIEST: If connected via USB, use the wireless icon") + "\n"
            + _("on the device selector to switch to Wi-Fi instantly.") + "\n\n"
            + _("FIRST-TIME PAIRING (without USB):") + "\n"
            + _("1. Enable Developer Options (same steps as USB above)") + "\n"
            + _("2. Go to Settings → Developer Options → Wireless Debugging") + "\n"
            + _("3. Tap 'Pair device with pairing code'") + "\n"
            + _("   ⚠ Use 'pairing CODE', NOT 'QR Code'") + "\n"
            + _("4. Note the IP:Port and 6-digit code shown on the phone") + "\n"
            + _("5. On the Wi-Fi tab, expand 'Pair new device'") + "\n"
            + _("6. Type the IP:Port and code, then tap 'Pair'") + "\n"
            + _("   💡 Or tap 'Find' to auto-fill the IP:Port") + "\n"
            + _("7. After pairing, tap 'Scan' to find the device") + "\n"
            + _("8. Click 'Start' on the Wi-Fi tab") + "\n\n"
            + "━━━ AirPlay (iPhone / iPad) ━━━\n"
            + _("Mirrors the entire screen (not just the camera).") + "\n"
            + _("1. Make sure both devices are on the same Wi-Fi network") + "\n"
            + _("2. Click 'Start' on the AirPlay tab") + "\n"
            + _("3. On your iPhone, open Control Center (swipe down from top-right)") + "\n"
            + _("4. Tap 'Screen Mirroring' and select 'BigCam'") + "\n\n"
            + "━━━ Browser (Android / iPhone) ━━━\n"
            + _("Works with any phone, no app required.") + "\n"
            + _("1. Connect both devices to the same Wi-Fi network") + "\n"
            + _("2. Click 'Start' on the Browser tab") + "\n"
            + _("3. Scan the QR code with your phone or type the URL") + "\n"
            + _("4. Accept the security warning in the browser") + "\n"
            + _("5. Tap 'Start' on the phone's browser page")
        )
        dialog.add_response("close", _("Close"))
        dialog.set_default_response("close")
        dialog.present(self)

    # ══════════════════════════════════════════════════════════════════
    #  MODE LOCKING (disable other tabs when a service is running)
    # ══════════════════════════════════════════════════════════════════

    def _set_mode_lock(self, active_mode: str | None) -> None:
        """Enable/disable controls so only one service runs at a time.

        When active_mode is set, the badge_number of that tab shows a
        green dot (via Adw badge) and other tabs' interactive controls
        are desensitized.
        """
        self._active_mode = active_mode

        # Browser tab
        wifi_locked = active_mode is not None and active_mode != _TAB_BROWSER
        self._wifi_start_btn.set_sensitive(
            not wifi_locked and self._wifi_available
        )
        self._port_row.set_sensitive(not wifi_locked)

        # USB tab
        usb_locked = active_mode is not None and active_mode != _TAB_USB
        self._usb_start_btn.set_sensitive(not usb_locked and self._usb_all_ok)

        # Wi-Fi Advanced tab
        adv_locked = active_mode is not None and active_mode != _TAB_WIFI_ADV
        self._scrcpy_start_btn.set_sensitive(
            not adv_locked and self._wadv_all_ok
        )
        self._scan_btn.set_sensitive(not adv_locked)
        self._pair_btn.set_sensitive(not adv_locked)
        self._discover_pair_btn.set_sensitive(not adv_locked)

        # AirPlay tab
        airplay_locked = active_mode is not None and active_mode != _TAB_AIRPLAY
        self._airplay_start_btn.set_sensitive(
            not airplay_locked and self._airplay_all_ok
        )

        # Attention dot on active tab (green indicator)
        for tab_id, page in self._tab_pages.items():
            page.set_needs_attention(
                active_mode is not None and tab_id == active_mode
            )

    # ══════════════════════════════════════════════════════════════════
    #  DRAWING
    # ══════════════════════════════════════════════════════════════════

    def _draw_dot(self, _area: Gtk.DrawingArea, cr: Any, w: int, h: int) -> None:
        r, g, b = self._dot_color
        cr.set_source_rgb(r, g, b)
        cr.arc(w / 2, h / 2, min(w, h) / 2, 0, 6.2832)
        cr.fill()

    def _set_dot_color(self, r: float, g: float, b: float) -> None:
        self._dot_color = (r, g, b)
        self._status_dot.queue_draw()

    def _set_status(self, text: str) -> None:
        self._status_label.set_label(text)

    def _set_resolution(self, w: int, h: int) -> None:
        if w and h:
            self._res_label.set_label(f"{w} × {h}")
        else:
            self._res_label.set_label("—")

    # ══════════════════════════════════════════════════════════════════
    #  QR CODE
    # ══════════════════════════════════════════════════════════════════

    def _generate_qr(self, url: str) -> GdkPixbuf.Pixbuf | None:
        qr_path = os.path.join(tempfile.gettempdir(), "bigcam-qr.png")

        if shutil.which("qrencode"):
            try:
                subprocess.run(
                    ["qrencode", "-o", qr_path, "-s", "6", "-m", "2",
                     "--foreground=000000", "--background=FFFFFF", url],
                    timeout=5,
                    capture_output=True,
                )
                return GdkPixbuf.Pixbuf.new_from_file(qr_path)
            except Exception as exc:
                log.warning("qrencode failed: %s", exc)

        try:
            import qrcode
            img = qrcode.make(url, box_size=6, border=2)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            loader = GdkPixbuf.PixbufLoader.new_with_type("png")
            loader.write(buf.read())
            loader.close()
            return loader.get_pixbuf()
        except ImportError:
            log.info("No QR generator available")
        except Exception as exc:
            log.warning("python-qrcode failed: %s", exc)

        return None

    # ══════════════════════════════════════════════════════════════════
    #  BACKEND SIGNAL CONNECTIONS
    # ══════════════════════════════════════════════════════════════════

    def _connect_backend_signals(self) -> None:
        self._server_sig_ids.append(
            self._server.connect("connected", self._on_wifi_connected)
        )
        self._server_sig_ids.append(
            self._server.connect("disconnected", self._on_wifi_disconnected)
        )
        self._server_sig_ids.append(
            self._server.connect("status-changed", self._on_wifi_status_changed)
        )

        self._scrcpy_sig_ids.append(
            self._scrcpy.connect("connected", self._on_scrcpy_connected)
        )
        self._scrcpy_sig_ids.append(
            self._scrcpy.connect("disconnected", self._on_scrcpy_disconnected)
        )
        self._scrcpy_sig_ids.append(
            self._scrcpy.connect("status-changed", self._on_scrcpy_status_changed)
        )

        self._airplay_sig_ids.append(
            self._airplay.connect("connected", self._on_airplay_connected)
        )
        self._airplay_sig_ids.append(
            self._airplay.connect("disconnected", self._on_airplay_disconnected)
        )
        self._airplay_sig_ids.append(
            self._airplay.connect("status-changed", self._on_airplay_status_changed)
        )

    def _restore_running_state(self) -> None:
        if self._server.running:
            self._wifi_start_btn.set_visible(False)
            self._wifi_stop_btn.set_visible(True)
            self._update_wifi_urls()
            if self._server.is_connected:
                self._set_dot_color(0.2, 0.78, 0.35)
                self._set_status(_("Connected via browser"))
                w, h = self._server.resolution
                self._set_resolution(w, h)
            else:
                self._set_dot_color(1.0, 0.76, 0.03)
                self._set_status(_("Waiting for connection…"))
            self._set_mode_lock(_TAB_BROWSER)
        elif self._scrcpy.running:
            # Determine which tab started scrcpy
            # If the scrcpy device is USB-connected, it came from USB tab
            serial = getattr(self._scrcpy, "_device_serial", "")
            is_usb = False
            if serial:
                try:
                    devs = ScrcpyCamera.list_devices()
                    is_usb = any(
                        d.serial == serial and d.transport == "usb"
                        for d in devs
                    )
                except Exception:
                    pass
            if is_usb:
                self._scrcpy_tab = _TAB_USB
                self._usb_start_btn.set_visible(False)
                self._usb_stop_btn.set_visible(True)
                self._set_mode_lock(_TAB_USB)
            else:
                self._scrcpy_tab = _TAB_WIFI_ADV
                self._scrcpy_start_btn.set_visible(False)
                self._scrcpy_stop_btn.set_visible(True)
                self._set_mode_lock(_TAB_WIFI_ADV)
            self._set_dot_color(0.2, 0.78, 0.35)
            self._set_status(_("Connected"))
        elif self._airplay.running:
            self._airplay_start_btn.set_visible(False)
            self._airplay_stop_btn.set_visible(True)
            self._set_dot_color(0.2, 0.78, 0.35)
            self._set_status(_("AirPlay connected"))
            self._set_mode_lock(_TAB_AIRPLAY)

    # ══════════════════════════════════════════════════════════════════
    #  BROWSER (Wi-Fi) HANDLERS
    # ══════════════════════════════════════════════════════════════════

    def _on_wifi_start(self, _btn: Gtk.Button) -> None:
        port = int(self._port_row.get_value())
        ok = self._server.start(port=port)
        if ok:
            self._wifi_start_btn.set_visible(False)
            self._wifi_stop_btn.set_visible(True)
            self._set_dot_color(1.0, 0.76, 0.03)
            self._set_status(_("Waiting for connection…"))
            self._update_wifi_urls()
            self._usb_poll_id = GLib.timeout_add_seconds(
                3, self._check_usb_tethering
            )
            self._set_mode_lock(_TAB_BROWSER)
        else:
            self._set_dot_color(0.85, 0.2, 0.2)
            self._set_status(_("Failed to start server"))

    def _on_wifi_stop(self, _btn: Gtk.Button) -> None:
        if self._usb_poll_id:
            GLib.source_remove(self._usb_poll_id)
            self._usb_poll_id = 0
        self._server.stop()
        self._wifi_start_btn.set_visible(True)
        self._wifi_stop_btn.set_visible(False)
        self._set_dot_color(0.6, 0.6, 0.6)
        self._set_status(_("Idle"))
        self._set_resolution(0, 0)
        self._wifi_url_row.set_subtitle(_("Start to see the address"))
        self._wifi_copy_btn.set_sensitive(False)
        self._qr_picture.set_paintable(None)
        self._set_mode_lock(None)
        self.emit("phone-disconnected")

    def _update_wifi_urls(self) -> None:
        url = self._server.get_url()
        self._wifi_url_row.set_subtitle(url)
        self._wifi_copy_btn.set_sensitive(True)

        def _gen() -> None:
            pixbuf = self._generate_qr(url)
            if pixbuf:
                texture = Gdk.Texture.new_for_pixbuf(pixbuf)
                GLib.idle_add(self._qr_picture.set_paintable, texture)

        threading.Thread(target=_gen, daemon=True).start()

    def _check_usb_tethering(self) -> bool:
        if not self._server.running:
            self._usb_poll_id = 0
            return False
        return True

    def _on_copy_wifi_url(self, _btn: Gtk.Button) -> None:
        url = self._wifi_url_row.get_subtitle()
        if url:
            self._copy_to_clipboard(url)

    def _on_wifi_connected(
        self, _server: PhoneCameraServer, w: int, h: int
    ) -> None:
        self._set_dot_color(0.2, 0.78, 0.35)
        self._set_status(_("Connected via browser"))
        self._set_resolution(w, h)
        self.emit("phone-connected", w, h)

    def _on_wifi_disconnected(self, _server: PhoneCameraServer) -> None:
        if self._server.running:
            self._set_dot_color(1.0, 0.76, 0.03)
            self._set_status(_("Waiting for connection…"))
        else:
            self._set_dot_color(0.6, 0.6, 0.6)
            self._set_status(_("Idle"))
        self._set_resolution(0, 0)
        self.emit("phone-disconnected")

    def _on_wifi_status_changed(
        self, _server: PhoneCameraServer, status: str
    ) -> None:
        if status == "listening":
            self._set_dot_color(1.0, 0.76, 0.03)
            self._set_status(_("Waiting for connection…"))
        elif status == "connected":
            self._set_dot_color(0.2, 0.78, 0.35)
            self._set_status(_("Connected via browser"))
        elif status in ("stopped", "disconnected"):
            self._set_dot_color(0.6, 0.6, 0.6)
            self._set_status(_("Idle"))

    # ══════════════════════════════════════════════════════════════════
    #  USB CABLE HANDLERS
    # ══════════════════════════════════════════════════════════════════

    def _on_refresh_usb_devices(self, _btn: Gtk.Button | None) -> None:
        def _on_done(
            devices: list,
            hint: str,
            hint_icon: str,
        ) -> None:
            usb_devs = [
                d for d in devices
                if d.transport == "usb" and d.state == "device"
            ]
            self._usb_only_devices = usb_devs
            if usb_devs:
                model = Gtk.StringList.new(
                    [f"{d.model} ({d.serial})" for d in usb_devs]
                )
                self._usb_device_row.set_model(model)
                self._usb_device_row.set_selected(0)
            else:
                self._usb_device_row.set_model(
                    Gtk.StringList.new([_(hint)])
                )

        def _thread() -> None:
            ScrcpyCamera.ensure_adb_server()
            devs = ScrcpyCamera.list_devices(include_unauthorized=True)

            usb_auth = [
                d for d in devs
                if d.transport == "usb" and d.state == "device"
            ]
            if usb_auth:
                GLib.idle_add(_on_done, devs, "", "")
                return

            usb_unauth = [
                d for d in devs
                if d.transport == "usb" and d.state == "unauthorized"
            ]
            if usb_unauth:
                name = usb_unauth[0].model
                hint = f"'{name}' — accept on phone"
                GLib.idle_add(
                    _on_done, devs, hint, "auth-fingerprint-symbolic"
                )
                return

            android_usb = ScrcpyCamera.detect_android_usb()
            if android_usb:
                name = android_usb[0].get("name", "Android")
                hint = f"'{name}' — enable USB Debugging"
                GLib.idle_add(
                    _on_done, devs, hint, "dialog-warning-symbolic"
                )
                return

            hint = "No device — connect USB cable"
            GLib.idle_add(_on_done, devs, hint, "")

        threading.Thread(target=_thread, daemon=True).start()

    def _on_usb_start(self, _btn: Gtk.Button) -> None:
        if not self._usb_only_devices:
            self._set_dot_color(0.85, 0.2, 0.2)
            self._set_status(_("No USB device connected"))
            return
        idx = self._usb_device_row.get_selected()
        if idx >= len(self._usb_only_devices):
            return

        device = self._usb_only_devices[idx]
        facing = (
            "back" if self._usb_facing_row.get_selected() == 0 else "front"
        )
        fps_model = self._usb_fps_row.get_model()
        fps = int(
            fps_model.get_string(self._usb_fps_row.get_selected()) or "30"
        )
        br_model = self._usb_bitrate_row.get_model()
        br_label = (
            br_model.get_string(self._usb_bitrate_row.get_selected())
            or "16 Mbps"
        )
        bitrate = br_label.split()[0] + "M"

        res_values = [720, 1080, 1440, 1920, 0]
        res_idx = self._usb_resolution_row.get_selected()
        max_size = (
            res_values[res_idx] if res_idx < len(res_values) else 1080
        )

        v4l2_dev = self._find_loopback_device()
        if not v4l2_dev:
            self._set_dot_color(0.85, 0.2, 0.2)
            self._set_status(_("No v4l2loopback device"))
            return

        self.emit("scrcpy-prepare")

        def _start_delayed() -> bool:
            self._set_dot_color(1.0, 0.76, 0.03)
            self._set_status(_("Starting…"))
            ok = self._scrcpy.start(
                device_serial=device.serial,
                v4l2_device=v4l2_dev,
                camera_facing=facing,
                fps=fps,
                bitrate=bitrate,
                max_size=max_size,
            )
            if ok:
                self._scrcpy_tab = _TAB_USB
                self._usb_start_btn.set_visible(False)
                self._usb_stop_btn.set_visible(True)
                self._set_mode_lock(_TAB_USB)
            return False

        GLib.timeout_add(500, _start_delayed)

    def _on_usb_stop(self, _btn: Gtk.Button) -> None:
        self._scrcpy.stop()
        self._scrcpy_tab = None
        VirtualCamera.release_device("phone:scrcpy")
        self._usb_start_btn.set_visible(True)
        self._usb_stop_btn.set_visible(False)
        self._set_dot_color(0.6, 0.6, 0.6)
        self._set_status(_("Idle"))
        self._set_resolution(0, 0)
        self._set_mode_lock(None)
        self.emit("scrcpy-disconnected")

    # ══════════════════════════════════════════════════════════════════
    #  WI-FI ADVANCED (ADB) HANDLERS
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def _discover_mdns(service_type: str) -> list[tuple[str, int, str]]:
        avahi = shutil.which("avahi-browse")
        if not avahi:
            return []
        try:
            result = subprocess.run(
                [avahi, "-trp", service_type],
                capture_output=True,
                text=True,
                timeout=8,
            )
        except (subprocess.TimeoutExpired, OSError):
            return []

        devices: list[tuple[str, int, str]] = []
        seen: set[str] = set()
        for line in result.stdout.splitlines():
            if not line.startswith("="):
                continue
            parts = line.split(";")
            if len(parts) < 9:
                continue
            ip = parts[7]
            try:
                port = int(parts[8])
            except ValueError:
                continue
            name = parts[3]
            txt = parts[9] if len(parts) > 9 else ""
            for token in txt.replace('"', "").split():
                if token.startswith("name="):
                    name = token[5:]
                    break
            key = f"{ip}:{port}"
            if key not in seen:
                seen.add(key)
                devices.append((ip, port, name))
        return devices

    def _on_scan_and_connect(self, _btn: Gtk.Button) -> None:
        self._scan_btn.set_sensitive(False)
        self._scan_btn.set_label(_("Scanning…"))
        self._set_dot_color(1.0, 0.76, 0.03)
        self._set_status(_("Scanning network…"))

        def _scan() -> None:
            devices = self._discover_mdns("_adb-tls-connect._tcp")
            GLib.idle_add(_on_found, devices)

        def _on_found(devices: list[tuple[str, int, str]]) -> None:
            self._discovered_devices = devices
            if not devices:
                self._scan_btn.set_sensitive(True)
                self._scan_btn.set_label(_("Scan"))
                self._set_dot_color(0.6, 0.6, 0.6)
                self._set_status(_("No devices found"))
                self._scan_row.set_subtitle(
                    _("No devices found. Try 'Pair new device'.")
                )
                return
            ip, port, name = devices[0]
            target = f"{ip}:{port}"
            self._scan_row.set_subtitle(
                _("Found: %s") % f"{name} ({target})"
            )
            self._set_status(_("Connecting to %s…") % name)

            def _connect() -> None:
                adb = shutil.which("adb") or "adb"
                try:
                    result = subprocess.run(
                        [adb, "connect", target],
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    output = (result.stdout + result.stderr).strip()
                    ok = "connected" in output.lower()
                except Exception as exc:
                    output = str(exc)
                    ok = False
                GLib.idle_add(_on_connected, ok, output, name)

            threading.Thread(target=_connect, daemon=True).start()

        def _on_connected(ok: bool, msg: str, name: str) -> None:
            self._scan_btn.set_sensitive(True)
            self._scan_btn.set_label(_("Scan"))
            if ok:
                self._set_dot_color(0.2, 0.78, 0.35)
                self._set_status(_("Connected to %s") % name)
                self._scan_row.set_subtitle(_("Ready to start camera"))
                GLib.timeout_add(800, self._on_refresh_devices, None)
            else:
                self._set_dot_color(0.85, 0.2, 0.2)
                self._set_status(_("Connection failed"))

        threading.Thread(target=_scan, daemon=True).start()

    def _on_discover_pairing(self, _btn: Gtk.Button) -> None:
        self._discover_pair_btn.set_sensitive(False)
        self._discover_pair_btn.set_label(_("Searching…"))

        def _scan() -> None:
            devices = self._discover_mdns("_adb-tls-pairing._tcp")
            GLib.idle_add(_on_done, devices)

        def _on_done(devices: list[tuple[str, int, str]]) -> None:
            self._discover_pair_btn.set_sensitive(True)
            self._discover_pair_btn.set_label(_("Find"))
            if devices:
                ip, port, name = devices[0]
                self._pair_ip_row.set_text(f"{ip}:{port}")
                self._discover_row.set_subtitle(
                    _("Found %s — enter the 6-digit code below") % name
                )
                self._pair_code_row.grab_focus()
            else:
                self._discover_row.set_subtitle(
                    _(
                        "Not found. Make sure you tapped "
                        "'Pair with pairing code' (not QR Code) "
                        "and that the code screen is still open. "
                        "You can also type the IP:Port manually."
                    )
                )

        threading.Thread(target=_scan, daemon=True).start()

    def _on_pair_wifi(self, _btn: Gtk.Button) -> None:
        host_port = self._pair_ip_row.get_text().strip()
        code = self._pair_code_row.get_text().strip()
        if not host_port or not code:
            self._set_dot_color(0.85, 0.2, 0.2)
            self._set_status(_("Enter IP:Port and pairing code"))
            return

        self._pair_btn.set_sensitive(False)
        self._set_dot_color(1.0, 0.76, 0.03)
        self._set_status(_("Pairing…"))

        def _pair() -> None:
            ok, msg = ScrcpyCamera.pair_wifi(host_port, code)
            GLib.idle_add(_on_done, ok, msg)

        def _on_done(ok: bool, msg: str) -> None:
            self._pair_btn.set_sensitive(True)
            if ok:
                self._set_dot_color(0.2, 0.78, 0.35)
                self._set_status(msg)
                GLib.timeout_add(1500, self._on_refresh_devices, None)
            else:
                self._set_dot_color(0.85, 0.2, 0.2)
                self._set_status(msg)

        threading.Thread(target=_pair, daemon=True).start()

    def _on_switch_to_wifi(self, _btn: Gtk.Button) -> None:
        if not self._devices:
            return
        idx = self._device_row.get_selected()
        if idx >= len(self._devices):
            return

        device = self._devices[idx]
        if device.transport == "tcpip":
            return

        self._adb_wifi_btn.set_sensitive(False)
        self._set_dot_color(1.0, 0.76, 0.03)
        self._set_status(_("Switching to Wi-Fi…"))

        def _switch() -> None:
            ok, result = ScrcpyCamera.switch_to_wifi(device.serial)
            GLib.idle_add(_on_done, ok, result)

        def _on_done(ok: bool, result: str) -> None:
            self._adb_wifi_btn.set_sensitive(True)
            if ok:
                self._set_dot_color(0.2, 0.78, 0.35)
                self._set_status(
                    _("Wi-Fi: %s (unplug USB)") % result
                )
                GLib.timeout_add(1500, self._on_refresh_devices, None)
            else:
                self._set_dot_color(0.85, 0.2, 0.2)
                self._set_status(_("Wi-Fi switch failed"))

        threading.Thread(target=_switch, daemon=True).start()

    def _on_refresh_devices(self, _btn: Gtk.Button | None) -> None:
        def _on_done(devices: list) -> None:
            self._devices = devices
            model = Gtk.StringList.new(
                [f"{d.model} ({d.transport})" for d in devices]
            )
            self._device_row.set_model(model)
            if devices:
                self._device_row.set_selected(0)
                self._adb_wifi_btn.set_visible(
                    devices[0].transport == "usb"
                )
            else:
                self._device_row.set_model(
                    Gtk.StringList.new([_("No devices found")])
                )

        def _thread() -> None:
            ScrcpyCamera.ensure_adb_server()
            devs = ScrcpyCamera.list_devices()
            GLib.idle_add(_on_done, devs)

        threading.Thread(target=_thread, daemon=True).start()

    def _on_scrcpy_start(self, _btn: Gtk.Button) -> None:
        if not self._devices:
            return
        idx = self._device_row.get_selected()
        if idx >= len(self._devices):
            return

        device = self._devices[idx]
        facing = (
            "back" if self._facing_row.get_selected() == 0 else "front"
        )
        fps_model = self._fps_row.get_model()
        fps = int(
            fps_model.get_string(self._fps_row.get_selected()) or "30"
        )
        br_model = self._bitrate_row.get_model()
        br_label = (
            br_model.get_string(self._bitrate_row.get_selected())
            or "16 Mbps"
        )
        bitrate = br_label.split()[0] + "M"

        res_values = [720, 1080, 1440, 1920, 0]
        res_idx = self._resolution_row.get_selected()
        max_size = (
            res_values[res_idx] if res_idx < len(res_values) else 1080
        )

        v4l2_dev = self._find_loopback_device()
        if not v4l2_dev:
            self._set_dot_color(0.85, 0.2, 0.2)
            self._set_status(_("No v4l2loopback device"))
            return

        self.emit("scrcpy-prepare")

        def _start_delayed() -> bool:
            self._set_dot_color(1.0, 0.76, 0.03)
            self._set_status(_("Starting…"))
            ok = self._scrcpy.start(
                device_serial=device.serial,
                v4l2_device=v4l2_dev,
                camera_facing=facing,
                fps=fps,
                bitrate=bitrate,
                max_size=max_size,
            )
            if ok:
                self._scrcpy_tab = _TAB_WIFI_ADV
                self._scrcpy_start_btn.set_visible(False)
                self._scrcpy_stop_btn.set_visible(True)
                self._set_mode_lock(_TAB_WIFI_ADV)
            return False

        GLib.timeout_add(500, _start_delayed)

    def _on_scrcpy_stop(self, _btn: Gtk.Button) -> None:
        self._scrcpy.stop()
        self._scrcpy_tab = None
        VirtualCamera.release_device("phone:scrcpy")
        self._scrcpy_start_btn.set_visible(True)
        self._scrcpy_stop_btn.set_visible(False)
        self._set_dot_color(0.6, 0.6, 0.6)
        self._set_status(_("Idle"))
        self._set_resolution(0, 0)
        self._set_mode_lock(None)
        self.emit("scrcpy-disconnected")

    def _on_scrcpy_connected(
        self, _scrcpy: ScrcpyCamera, w: int, h: int
    ) -> None:
        self._set_dot_color(0.2, 0.78, 0.35)
        if self._scrcpy_tab == _TAB_USB:
            self._set_status(_("Connected via USB"))
        else:
            self._set_status(_("Connected via Wi-Fi"))
        self._set_resolution(w, h)
        self.emit("scrcpy-connected", w, h)

    def _on_scrcpy_disconnected(self, _scrcpy: ScrcpyCamera) -> None:
        self._set_dot_color(0.85, 0.2, 0.2)
        self._set_status(_("Disconnected"))
        self._set_resolution(0, 0)
        # Reset only the tab that started scrcpy
        if self._scrcpy_tab == _TAB_USB:
            self._usb_start_btn.set_visible(True)
            self._usb_stop_btn.set_visible(False)
        elif self._scrcpy_tab == _TAB_WIFI_ADV:
            self._scrcpy_start_btn.set_visible(True)
            self._scrcpy_stop_btn.set_visible(False)
        else:
            # Unknown source — reset both
            self._scrcpy_start_btn.set_visible(True)
            self._scrcpy_stop_btn.set_visible(False)
            self._usb_start_btn.set_visible(True)
            self._usb_stop_btn.set_visible(False)
        self._scrcpy_tab = None
        self._set_mode_lock(None)
        self.emit("scrcpy-disconnected")

    def _on_scrcpy_status_changed(
        self, _scrcpy: ScrcpyCamera, status: str
    ) -> None:
        if status == "starting":
            self._set_dot_color(1.0, 0.76, 0.03)
            self._set_status(_("Starting…"))
        elif status == "connected":
            self._set_dot_color(0.2, 0.78, 0.35)
            if self._scrcpy_tab == _TAB_USB:
                self._set_status(_("Connected via USB"))
            else:
                self._set_status(_("Connected via Wi-Fi"))
        elif status in ("stopped", "disconnected"):
            self._set_dot_color(0.6, 0.6, 0.6)
            self._set_status(_("Idle"))
        elif status == "error":
            self._set_dot_color(0.85, 0.2, 0.2)
            self._set_status(_("Error"))

    # ══════════════════════════════════════════════════════════════════
    #  AIRPLAY HANDLERS
    # ══════════════════════════════════════════════════════════════════

    def _on_airplay_start(self, _btn: Gtk.Button) -> None:
        if not self._airplay:
            return

        v4l2_dev = VirtualCamera.allocate_device("phone:airplay")
        if not v4l2_dev:
            self._set_dot_color(0.85, 0.2, 0.2)
            self._set_status(_("No v4l2loopback device"))
            return

        server_name = (
            self._airplay_name_row.get_text().strip() or "BigCam"
        )

        res_map = {"720p": 720, "1080p": 1080, "1440p": 1440}
        res_model = self._airplay_res_row.get_model()
        res_label = (
            res_model.get_string(self._airplay_res_row.get_selected())
            or "1080p"
        )
        max_size = res_map.get(res_label, 1080)

        fps_model = self._airplay_fps_row.get_model()
        fps = int(
            fps_model.get_string(self._airplay_fps_row.get_selected())
            or "30"
        )

        rotate_idx = self._airplay_rotate_row.get_selected()
        rotate_map = {0: "", 1: "R", 2: "L", 3: "I"}
        rotation = rotate_map.get(rotate_idx, "")

        self.emit("airplay-prepare")

        def _start_delayed() -> bool:
            self._set_dot_color(1.0, 0.76, 0.03)
            self._set_status(_("Starting AirPlay…"))
            ok = self._airplay.start(
                v4l2_device=v4l2_dev,
                server_name=server_name,
                max_size=max_size,
                fps=fps,
                rotation=rotation,
            )
            if ok:
                self._airplay_start_btn.set_visible(False)
                self._airplay_stop_btn.set_visible(True)
                self._set_mode_lock(_TAB_AIRPLAY)
            else:
                VirtualCamera.release_device("phone:airplay")
                self._set_dot_color(0.85, 0.2, 0.2)
                self._set_status(
                    _("Failed to start AirPlay. Try again.")
                )
            return False

        GLib.timeout_add(1500, _start_delayed)

    def _on_airplay_stop(self, _btn: Gtk.Button) -> None:
        if self._airplay:
            self._airplay.stop()
        VirtualCamera.release_device("phone:airplay")
        self._airplay_start_btn.set_visible(True)
        self._airplay_stop_btn.set_visible(False)
        self._set_dot_color(0.6, 0.6, 0.6)
        self._set_status(_("Idle"))
        self._set_resolution(0, 0)
        self._set_mode_lock(None)
        self.emit("airplay-disconnected")

    def _on_airplay_connected(
        self, _receiver: AirPlayReceiver, w: int, h: int
    ) -> None:
        self._set_dot_color(0.2, 0.78, 0.35)
        self._set_status(_("AirPlay connected"))
        self._set_resolution(w, h)
        self.emit("airplay-connected", w, h)

    def _on_airplay_disconnected(
        self, _receiver: AirPlayReceiver
    ) -> None:
        self._set_dot_color(1.0, 0.76, 0.03)
        self._set_status(_("Waiting for AirPlay connection…"))
        self._set_resolution(0, 0)

    def _on_airplay_status_changed(
        self, _receiver: AirPlayReceiver, status: str
    ) -> None:
        self._set_status(status)

    # ══════════════════════════════════════════════════════════════════
    #  DIALOG LIFECYCLE
    # ══════════════════════════════════════════════════════════════════

    def _on_dialog_closed(self, _dialog: Adw.Dialog) -> None:
        if self._usb_poll_id:
            GLib.source_remove(self._usb_poll_id)
            self._usb_poll_id = 0
        for sid in self._server_sig_ids:
            self._server.disconnect(sid)
        self._server_sig_ids.clear()
        for sid in self._scrcpy_sig_ids:
            self._scrcpy.disconnect(sid)
        self._scrcpy_sig_ids.clear()
        if self._airplay:
            for sid in self._airplay_sig_ids:
                self._airplay.disconnect(sid)
            self._airplay_sig_ids.clear()

    # ══════════════════════════════════════════════════════════════════
    #  HELPERS
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def _copy_to_clipboard(text: str) -> None:
        display = Gdk.Display.get_default()
        if display:
            clipboard = display.get_clipboard()
            clipboard.set(text)

    @property
    def scrcpy(self) -> ScrcpyCamera:
        return self._scrcpy

    @property
    def server(self) -> PhoneCameraServer:
        return self._server

    @staticmethod
    def _find_loopback_device() -> str:
        dev = VirtualCamera.allocate_device("phone:scrcpy")
        if dev:
            return dev
        for n in (10, 11, 12, 13):
            path = f"/dev/video{n}"
            if os.path.exists(path):
                return path
        return ""
