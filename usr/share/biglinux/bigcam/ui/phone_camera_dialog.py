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

from gi.repository import Adw, Gdk, GdkPixbuf, GLib, Gtk, GObject, Pango

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
        # Guard against widget access after dialog destroyed
        self._closed: bool = False

        self._build_ui()
        self._connect_backend_signals()
        self.connect("closed", self._on_dialog_closed)
        self._restore_running_state()

    # ══════════════════════════════════════════════════════════════════
    #  UI BUILD
    # ══════════════════════════════════════════════════════════════════

    def _build_ui(self) -> None:
        # Dynamic CSS for needs-attention indicator color (matches status dot)
        self._tab_dot_css = Gtk.CssProvider()
        self._tab_dot_css.load_from_string(
            "viewswitcher indicator { background: rgba(153,153,153,1); }"
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            self._tab_dot_css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

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
            "symbolic-phone-android",
        )

        # Tab 3: USB
        usb_page = self._stack.add_titled_with_icon(
            self._build_usb_page(),
            _TAB_USB,
            _("USB"),
            "symbolic-phone-android",
        )

        # Tab 4: AirPlay
        airplay_page = self._stack.add_titled_with_icon(
            self._build_airplay_page(),
            _TAB_AIRPLAY,
            _("AirPlay"),
            "symbolic-phone-apple",
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
        self._status_label.set_ellipsize(Pango.EllipsizeMode.END)
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
        v4l2_ok = VirtualCamera.is_available()
        self._usb_all_ok = scrcpy_ok and adb_ok and v4l2_ok

        if not self._usb_all_ok:
            content.append(self._make_missing_banner(scrcpy_ok, adb_ok, v4l2_ok))

        # ── Device selector ──────────────────────────────────────────
        dev_group = Adw.PreferencesGroup()

        self._usb_device_row = Adw.ComboRow(title=_("Device"))
        self._usb_device_row.set_model(Gtk.StringList.new([_("Searching…")]))
        dev_icon = Gtk.Image.new_from_icon_name("symbolic-phone-android")
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
        usb_qual_icon = Gtk.Image.new_from_icon_name("video-display-symbolic")
        usb_qual_icon.set_valign(Gtk.Align.CENTER)
        self._usb_resolution_row.add_prefix(usb_qual_icon)
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

        # ── Spacer + Inline status + Start button ────────────────────
        content.append(Gtk.Box(vexpand=True))  # push button down

        self._usb_inline_status = Gtk.Label()
        self._usb_inline_status.set_halign(Gtk.Align.CENTER)
        self._usb_inline_status.set_wrap(True)
        self._usb_inline_status.set_wrap_mode(2)  # PANGO_WRAP_WORD_CHAR
        self._usb_inline_status.add_css_class("caption")
        self._usb_inline_status.set_visible(False)
        content.append(self._usb_inline_status)

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
        v4l2_ok = VirtualCamera.is_available()
        self._wadv_all_ok = scrcpy_ok and adb_ok and v4l2_ok

        if not self._wadv_all_ok:
            content.append(self._make_missing_banner(scrcpy_ok, adb_ok, v4l2_ok))

        # ── Device selector ──────────────────────────────────────────
        dev_group = Adw.PreferencesGroup()

        self._device_row = Adw.ComboRow(title=_("Device"))
        self._device_row.set_model(Gtk.StringList.new([_("Searching…")]))
        dev_icon = Gtk.Image.new_from_icon_name("symbolic-phone-android")
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

        # 6-digit pairing code first, then IP:Port
        self._pair_code_row = Adw.EntryRow(
            title=_("6-digit code (shown on phone screen)"),
        )
        self._pair_code_row.set_input_purpose(Gtk.InputPurpose.NUMBER)
        self._pair_expander.add_row(self._pair_code_row)

        self._pair_ip_row = Adw.EntryRow(
            title=_("IP:Port (shown on phone screen)"),
        )
        self._pair_ip_row.set_input_purpose(Gtk.InputPurpose.FREE_FORM)
        self._pair_expander.add_row(self._pair_ip_row)

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

        # Inline status feedback above Start button
        self._scrcpy_inline_status = Gtk.Label()
        self._scrcpy_inline_status.set_halign(Gtk.Align.CENTER)
        self._scrcpy_inline_status.set_wrap(True)
        self._scrcpy_inline_status.set_wrap_mode(2)  # PANGO_WRAP_WORD_CHAR
        self._scrcpy_inline_status.add_css_class("caption")
        self._scrcpy_inline_status.set_visible(False)
        content.append(self._scrcpy_inline_status)

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
        v4l2_ok = VirtualCamera.is_available()
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
        name_icon = Gtk.Image.new_from_icon_name("symbolic-phone-apple")
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
        air_qual_icon = Gtk.Image.new_from_icon_name("video-display-symbolic")
        air_qual_icon.set_valign(Gtk.Align.CENTER)
        self._airplay_res_row.add_prefix(air_qual_icon)
        quality_group.add(self._airplay_res_row)

        self._airplay_fps_row = Adw.ComboRow(title=_("FPS"))
        self._airplay_fps_row.set_model(Gtk.StringList.new(["30", "60"]))
        air_fps_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        air_fps_icon.set_valign(Gtk.Align.CENTER)
        self._airplay_fps_row.add_prefix(air_fps_icon)
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
        air_rot_icon = Gtk.Image.new_from_icon_name("view-refresh-symbolic")
        air_rot_icon.set_valign(Gtk.Align.CENTER)
        self._airplay_rotate_row.add_prefix(air_rot_icon)
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
        port_icon = Gtk.Image.new_from_icon_name("network-server-symbolic")
        port_icon.set_valign(Gtk.Align.CENTER)
        self._port_row.add_prefix(port_icon)
        port_group.add(self._port_row)
        content.append(port_group)

        # ── Spacer + Start ───────────────────────────────────────────
        content.append(Gtk.Box(vexpand=True))

        # Inline status feedback above Start button
        self._wifi_inline_status = Gtk.Label()
        self._wifi_inline_status.set_halign(Gtk.Align.CENTER)
        self._wifi_inline_status.set_wrap(True)
        self._wifi_inline_status.set_wrap_mode(2)  # PANGO_WRAP_WORD_CHAR
        self._wifi_inline_status.add_css_class("caption")
        self._wifi_inline_status.set_visible(False)
        content.append(self._wifi_inline_status)

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
        dialog = Adw.Dialog()
        dialog.set_title(_("How to connect your phone"))
        dialog.set_content_width(520)
        dialog.set_content_height(480)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        view_switcher = Adw.ViewSwitcher()
        view_switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(view_switcher)
        toolbar_view.add_top_bar(header)

        stack = Adw.ViewStack()
        view_switcher.set_stack(stack)

        def _section_header(icon_name: str, title: str, description: str) -> Gtk.Box:
            """Compact header with small icon, title and subtitle (no scroll)."""
            box = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=4,
                halign=Gtk.Align.CENTER,
                margin_top=12,
                margin_bottom=4,
            )
            icon = Gtk.Image.new_from_icon_name(icon_name)
            icon.set_pixel_size(32)
            icon.add_css_class("dim-label")
            box.append(icon)

            lbl_title = Gtk.Label(label=title)
            lbl_title.add_css_class("title-4")
            box.append(lbl_title)

            lbl_desc = Gtk.Label(label=description)
            lbl_desc.add_css_class("dim-label")
            lbl_desc.add_css_class("caption")
            box.append(lbl_desc)

            return box

        def _step_row(num: int, text: str) -> Adw.ActionRow:
            row = Adw.ActionRow(title=text)
            row.set_title_lines(0)
            lbl = Gtk.Label(label=str(num))
            lbl.add_css_class("accent")
            lbl.add_css_class("heading")
            lbl.set_valign(Gtk.Align.CENTER)
            lbl.set_size_request(28, 28)
            row.add_prefix(lbl)
            return row

        def _tip_row(text: str) -> Adw.ActionRow:
            row = Adw.ActionRow(title=text)
            row.set_title_lines(0)
            icon = Gtk.Image.new_from_icon_name("dialog-information-symbolic")
            icon.set_valign(Gtk.Align.CENTER)
            icon.add_css_class("accent")
            row.add_prefix(icon)
            return row

        def _warn_row(text: str) -> Adw.ActionRow:
            row = Adw.ActionRow(title=text)
            row.set_title_lines(0)
            icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
            icon.set_valign(Gtk.Align.CENTER)
            icon.add_css_class("warning")
            row.add_prefix(icon)
            return row

        # ── Browser tab (1st – matches main tab order) ───────────────
        brw_scroll = Gtk.ScrolledWindow(
            vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        brw_clamp = Adw.Clamp(maximum_size=460, tightening_threshold=360)
        brw_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16, margin_top=4, margin_bottom=24,
            margin_start=16, margin_end=16,
        )
        brw_box.append(_section_header(
            "web-browser-symbolic",
            _("Browser (Android / iPhone)"),
            _("Works with any phone, no app required."),
        ))

        brw_group = Adw.PreferencesGroup()
        brw_group.add(_step_row(1, _("Connect both devices to the same Wi-Fi network")))
        brw_group.add(_step_row(2, _("Click 'Start' on the Browser tab")))
        brw_group.add(_step_row(3, _("Scan the QR code with your phone or type the URL")))
        brw_group.add(_step_row(4, _("Accept the security warning in the browser")))
        brw_group.add(_step_row(5, _("Tap 'Start' on the phone's browser page")))
        brw_box.append(brw_group)

        brw_clamp.set_child(brw_box)
        brw_scroll.set_child(brw_clamp)
        stack.add_titled_with_icon(
            brw_scroll, "browser", _("Browser"), "web-browser-symbolic"
        )

        # ── Wi-Fi tab (2nd) ──────────────────────────────────────────
        wifi_scroll = Gtk.ScrolledWindow(
            vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        wifi_clamp = Adw.Clamp(maximum_size=460, tightening_threshold=360)
        wifi_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16, margin_top=4, margin_bottom=24,
            margin_start=16, margin_end=16,
        )
        wifi_box.append(_section_header(
            "symbolic-phone-android",
            _("Wi-Fi (Android 11+)"),
            _("No cable needed after the first setup."),
        ))

        wifi_tip_group = Adw.PreferencesGroup(title=_("Quick method"))
        wifi_tip_group.add(_tip_row(
            _("If connected via USB, use the wireless icon on the "
              "device selector to switch to Wi-Fi instantly.")
        ))
        wifi_box.append(wifi_tip_group)

        wifi_pair_group = Adw.PreferencesGroup(
            title=_("First-time pairing (without USB)")
        )
        wifi_pair_group.add(_step_row(1, _("Enable Developer Options (same steps as USB above)")))
        wifi_pair_group.add(_step_row(2, _("Go to Settings → Developer Options → Wireless Debugging")))
        wifi_pair_group.add(_step_row(3, _("Tap 'Pair device with pairing code'")))
        wifi_pair_group.add(_warn_row(_("Use 'pairing CODE', NOT 'QR Code'")))
        wifi_pair_group.add(_step_row(4, _("Note the IP:Port and 6-digit code shown on the phone")))
        wifi_pair_group.add(_step_row(5, _("On the Wi-Fi tab, expand 'Pair new device'")))
        wifi_pair_group.add(_step_row(6, _("Type the IP:Port and code, then tap 'Pair'")))
        wifi_pair_group.add(_tip_row(_("Or tap 'Find' to auto-fill the IP:Port")))
        wifi_pair_group.add(_step_row(7, _("After pairing, tap 'Scan' to find the device")))
        wifi_pair_group.add(_step_row(8, _("Click 'Start' on the Wi-Fi tab")))
        wifi_box.append(wifi_pair_group)

        wifi_clamp.set_child(wifi_box)
        wifi_scroll.set_child(wifi_clamp)
        stack.add_titled_with_icon(
            wifi_scroll, "wifi", _("Wi-Fi"), "symbolic-phone-android"
        )

        # ── USB tab (3rd) ────────────────────────────────────────────
        usb_scroll = Gtk.ScrolledWindow(
            vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        usb_clamp = Adw.Clamp(maximum_size=460, tightening_threshold=360)
        usb_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16, margin_top=4, margin_bottom=24,
            margin_start=16, margin_end=16,
        )
        usb_box.append(_section_header(
            "symbolic-phone-android",
            _("USB (Android)"),
            _("The easiest and fastest method."),
        ))

        usb_group = Adw.PreferencesGroup()
        usb_group.add(_step_row(1, _("On your Android phone, go to Settings → About Phone")))
        usb_group.add(_step_row(2, _("Tap 'Build Number' 7 times to unlock Developer Options")))
        usb_group.add(_step_row(3, _("Go to Settings → Developer Options → enable 'USB Debugging'")))
        usb_group.add(_step_row(4, _("Connect the USB cable to the computer")))
        usb_group.add(_step_row(5, _("Accept the USB Debugging prompt on the phone screen")))
        usb_group.add(_step_row(6, _("Click 'Start' on the USB tab")))
        usb_box.append(usb_group)

        usb_clamp.set_child(usb_box)
        usb_scroll.set_child(usb_clamp)
        stack.add_titled_with_icon(
            usb_scroll, "usb", _("USB"), "symbolic-phone-android"
        )

        # ── AirPlay tab (4th) ────────────────────────────────────────
        air_scroll = Gtk.ScrolledWindow(
            vexpand=True,
            hscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        air_clamp = Adw.Clamp(maximum_size=460, tightening_threshold=360)
        air_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16, margin_top=4, margin_bottom=24,
            margin_start=16, margin_end=16,
        )
        air_box.append(_section_header(
            "symbolic-phone-apple",
            _("AirPlay (iPhone / iPad)"),
            _("Mirrors the entire screen (not just the camera)."),
        ))

        air_group = Adw.PreferencesGroup()
        air_group.add(_step_row(1, _("Make sure both devices are on the same Wi-Fi network")))
        air_group.add(_step_row(2, _("Click 'Start' on the AirPlay tab")))
        air_group.add(_step_row(3, _("On your iPhone, open Control Center (swipe down from top-right)")))
        air_group.add(_step_row(4, _("Tap 'Screen Mirroring' and select 'BigCam'")))
        air_box.append(air_group)

        air_clamp.set_child(air_box)
        air_scroll.set_child(air_clamp)
        stack.add_titled_with_icon(
            air_scroll, "airplay", _("AirPlay"), "symbolic-phone-apple"
        )

        toolbar_view.set_content(stack)
        dialog.set_child(toolbar_view)

        # Open the info dialog on the same tab the user is viewing
        current = self._stack.get_visible_child_name()
        tab_map = {
            _TAB_BROWSER: "browser",
            _TAB_WIFI_ADV: "wifi",
            _TAB_USB: "usb",
            _TAB_AIRPLAY: "airplay",
        }
        info_tab = tab_map.get(current)
        if info_tab:
            stack.set_visible_child_name(info_tab)

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
        # Sync the tab needs-attention indicator color
        ri, gi, bi = int(r * 255), int(g * 255), int(b * 255)
        self._tab_dot_css.load_from_string(
            f"viewswitcher indicator {{ background: rgb({ri},{gi},{bi}); }}"
        )

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
        self._wifi_inline_status.remove_css_class("error")
        self._wifi_inline_status.remove_css_class("success")
        self._wifi_inline_status.add_css_class("dim-label")
        self._wifi_inline_status.set_label(_("Starting server…"))
        self._wifi_inline_status.set_visible(True)
        self._wifi_start_btn.set_sensitive(False)

        def _do_start() -> tuple[bool, str]:
            return self._server.start(port=port)

        def _on_result(result: tuple[bool, str]) -> None:
            ok, msg = result
            if ok:
                self._wifi_start_btn.set_visible(False)
                self._wifi_stop_btn.set_visible(True)
                self._wifi_inline_status.remove_css_class("dim-label")
                self._wifi_inline_status.add_css_class("success")
                self._wifi_inline_status.set_label(
                    _("Server listening on port %d") % port
                )
                self._set_dot_color(1.0, 0.76, 0.03)
                self._set_status(_("Waiting for connection…"))
                self._update_wifi_urls()
                self._usb_poll_id = GLib.timeout_add_seconds(
                    3, self._check_usb_tethering
                )
                self._set_mode_lock(_TAB_BROWSER)
                # Auto-hide success message after 5s
                GLib.timeout_add_seconds(
                    5, lambda: self._wifi_inline_status.set_visible(False) or False
                )
            else:
                self._wifi_start_btn.set_sensitive(True)
                self._wifi_inline_status.remove_css_class("dim-label")
                self._wifi_inline_status.add_css_class("error")
                self._wifi_inline_status.set_label(
                    msg or _("Failed to start server")
                )
                self._set_dot_color(0.85, 0.2, 0.2)
                self._set_status(_("Failed to start server"))

        def _start_thread() -> None:
            result = _do_start()
            GLib.idle_add(_on_result, result)

        threading.Thread(target=_start_thread, daemon=True).start()

    def _on_wifi_stop(self, _btn: Gtk.Button) -> None:
        if self._usb_poll_id:
            GLib.source_remove(self._usb_poll_id)
            self._usb_poll_id = 0
        self._server.stop()
        self._wifi_start_btn.set_visible(True)
        self._wifi_stop_btn.set_visible(False)
        self._wifi_inline_status.set_visible(False)
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
        self._usb_inline_status.remove_css_class("success")
        if not self._usb_only_devices:
            self._set_dot_color(0.85, 0.2, 0.2)
            self._set_status(_("No USB device connected"))
            self._usb_inline_status.remove_css_class("dim-label")
            self._usb_inline_status.add_css_class("error")
            self._usb_inline_status.set_label(
                _("No device found. Connect via USB cable and enable USB Debugging, then tap 'Refresh'.")
            )
            self._usb_inline_status.set_visible(True)
            return
        idx = self._usb_device_row.get_selected()
        if idx >= len(self._usb_only_devices):
            self._usb_inline_status.remove_css_class("dim-label")
            self._usb_inline_status.add_css_class("error")
            self._usb_inline_status.set_label(
                _("Selected device is no longer available. Tap 'Refresh' to update.")
            )
            self._usb_inline_status.set_visible(True)
            return
        self._usb_inline_status.set_visible(False)

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
        self._usb_inline_status.set_visible(False)
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
                # Auto scan + connect + start after successful pairing
                GLib.timeout_add(1000, self._auto_scan_and_start)
            else:
                self._set_dot_color(0.85, 0.2, 0.2)
                self._set_status(msg)

        threading.Thread(target=_pair, daemon=True).start()

    def _auto_scan_and_start(self) -> bool:
        """After pairing, scan for the device and start the camera."""
        self._set_dot_color(1.0, 0.76, 0.03)
        self._set_status(_("Scanning for paired device…"))

        def _scan() -> None:
            devices = self._discover_mdns("_adb-tls-connect._tcp")
            GLib.idle_add(_on_found, devices)

        def _on_found(devices: list[tuple[str, int, str]]) -> None:
            if not devices:
                self._set_dot_color(0.6, 0.6, 0.6)
                self._set_status(_("No devices found after pairing"))
                return
            ip, port, name = devices[0]
            target = f"{ip}:{port}"
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
                GLib.idle_add(_on_connected, ok, name)

            threading.Thread(target=_connect, daemon=True).start()

        def _on_connected(ok: bool, name: str) -> None:
            if ok:
                self._set_dot_color(0.2, 0.78, 0.35)
                self._set_status(_("Connected to %s — starting camera…") % name)
                # Refresh device list then start
                def _refresh_and_start() -> None:
                    ScrcpyCamera.ensure_adb_server()
                    devs = ScrcpyCamera.list_devices()
                    GLib.idle_add(_do_start, devs)

                threading.Thread(target=_refresh_and_start, daemon=True).start()
            else:
                self._set_dot_color(0.85, 0.2, 0.2)
                self._set_status(_("Connection failed after pairing"))

        def _do_start(devices: list) -> None:
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
                # Auto-start the camera
                self._on_scrcpy_start(None)
            else:
                self._set_dot_color(0.85, 0.2, 0.2)
                self._set_status(_("No device available to start"))

        threading.Thread(target=_scan, daemon=True).start()
        return False  # Don't repeat GLib.timeout

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
        self._scrcpy_inline_status.remove_css_class("success")
        if not self._devices:
            self._scrcpy_inline_status.remove_css_class("dim-label")
            self._scrcpy_inline_status.add_css_class("error")
            self._scrcpy_inline_status.set_label(
                _("No device found. Pair a device first, then tap 'Scan'.")
            )
            self._scrcpy_inline_status.set_visible(True)
            return
        idx = self._device_row.get_selected()
        if idx >= len(self._devices):
            self._scrcpy_inline_status.remove_css_class("dim-label")
            self._scrcpy_inline_status.add_css_class("error")
            self._scrcpy_inline_status.set_label(
                _("Selected device is no longer available. Tap 'Scan' to refresh.")
            )
            self._scrcpy_inline_status.set_visible(True)
            return
        self._scrcpy_inline_status.set_visible(False)

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
        if not self._closed:
            self._set_dot_color(0.2, 0.78, 0.35)
            if self._scrcpy_tab == _TAB_USB:
                self._set_status(_("Connected via USB"))
            else:
                self._set_status(_("Connected via Wi-Fi"))
            self._set_resolution(w, h)
        self.emit("scrcpy-connected", w, h)

    def _on_scrcpy_disconnected(self, _scrcpy: ScrcpyCamera) -> None:
        if not self._closed:
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
        if self._closed:
            return
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
        if not self._closed:
            self._set_dot_color(0.2, 0.78, 0.35)
            self._set_status(_("AirPlay connected"))
            self._set_resolution(w, h)
        self.emit("airplay-connected", w, h)

    def _on_airplay_disconnected(
        self, _receiver: AirPlayReceiver
    ) -> None:
        if not self._airplay.running:
            # UxPlay process died — full cleanup
            VirtualCamera.release_device("phone:airplay")
            if not self._closed:
                self._airplay_start_btn.set_visible(True)
                self._airplay_stop_btn.set_visible(False)
                self._set_dot_color(0.6, 0.6, 0.6)
                self._set_status(_("AirPlay stopped unexpectedly"))
                self._set_resolution(0, 0)
                self._set_mode_lock(None)
            self.emit("airplay-disconnected")
        else:
            # Client disconnected but UxPlay still running (can reconnect)
            if not self._closed:
                self._set_dot_color(1.0, 0.76, 0.03)
                self._set_status(_("Waiting for AirPlay connection…"))
                self._set_resolution(0, 0)
            # Tell window to restore previous camera while waiting
            self.emit("airplay-disconnected")

    def _on_airplay_status_changed(
        self, _receiver: AirPlayReceiver, status: str
    ) -> None:
        if not self._closed:
            self._set_status(status)

    # ══════════════════════════════════════════════════════════════════
    #  DIALOG LIFECYCLE
    # ══════════════════════════════════════════════════════════════════

    def _on_dialog_closed(self, _dialog: Adw.Dialog) -> None:
        self._closed = True
        if self._usb_poll_id:
            GLib.source_remove(self._usb_poll_id)
            self._usb_poll_id = 0
        # Remove the CSS provider we added to the display
        display = Gdk.Display.get_default()
        if display and self._tab_dot_css:
            Gtk.StyleContext.remove_provider_for_display(display, self._tab_dot_css)
        for sid in self._server_sig_ids:
            self._server.disconnect(sid)
        self._server_sig_ids.clear()
        # Only disconnect scrcpy signals if scrcpy is NOT running
        if not self._scrcpy.running:
            for sid in self._scrcpy_sig_ids:
                self._scrcpy.disconnect(sid)
            self._scrcpy_sig_ids.clear()
        # Only disconnect airplay signals if airplay is NOT running
        if self._airplay and not self._airplay.running:
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
        # Fallback: find a v4l2loopback device by checking driver name
        for n in range(20):
            path = f"/dev/video{n}"
            if not os.path.exists(path):
                continue
            try:
                result = subprocess.run(
                    ["v4l2-ctl", "-d", path, "--info"],
                    capture_output=True, text=True, timeout=2,
                )
                if "v4l2 loopback" in result.stdout.lower():
                    return path
            except (OSError, subprocess.TimeoutExpired):
                continue
        return ""
