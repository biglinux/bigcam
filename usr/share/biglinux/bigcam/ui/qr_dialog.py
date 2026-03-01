"""QR Code result dialog — interprets QR type and shows appropriate actions."""

from __future__ import annotations

import os
import re
import time as _time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, GLib, Gdk, Gio

from utils.i18n import _


class QrType(Enum):
    URL = auto()
    VCARD = auto()
    TEXT = auto()
    PHONE = auto()
    SMS = auto()
    EMAIL = auto()
    WIFI = auto()
    GEO = auto()
    CALENDAR = auto()
    APP_STORE = auto()
    PAYMENT_PIX = auto()
    CRYPTO = auto()
    SOCIAL = auto()
    TOTP = auto()


_SOCIAL_DOMAINS = (
    "facebook.com", "instagram.com", "twitter.com", "x.com",
    "linkedin.com", "tiktok.com", "youtube.com", "github.com",
    "t.me", "telegram.me", "wa.me", "whatsapp.com",
    "threads.net", "mastodon.social", "bsky.app",
)

_APP_STORE_DOMAINS = (
    "play.google.com", "apps.apple.com", "itunes.apple.com",
    "f-droid.org", "flathub.org", "snapcraft.io",
)


@dataclass
class QrResult:
    qr_type: QrType
    raw: str
    title: str = ""
    details: dict[str, str] = field(default_factory=dict)


def parse_qr(data: str) -> QrResult:
    """Parse raw QR code data and identify its type."""
    stripped = data.strip()
    lower = stripped.lower()

    # Phone
    if lower.startswith("tel:"):
        number = stripped[4:]
        return QrResult(QrType.PHONE, stripped, _("Phone"), {"number": number})

    # SMS
    if lower.startswith("smsto:") or lower.startswith("sms:"):
        parts = stripped.split(":", 1)[1]
        number, *body = parts.split(":", 1)
        d = {"number": number}
        if body:
            d["message"] = body[0]
        return QrResult(QrType.SMS, stripped, _("SMS"), d)

    # Email (mailto:)
    if lower.startswith("mailto:"):
        email_part = stripped[7:]
        addr = email_part.split("?")[0]
        d: dict[str, str] = {"address": addr, "url": stripped}
        if "?" in email_part:
            params = email_part.split("?", 1)[1]
            for p in params.split("&"):
                if "=" in p:
                    k, v = p.split("=", 1)
                    d[k.lower()] = v
        return QrResult(QrType.EMAIL, stripped, _("E-mail"), d)

    # Email (MATMSG format)
    if lower.startswith("matmsg:"):
        d = {}
        content = stripped[7:]
        if content.endswith(";;"):
            content = content[:-1]
        for part in content.split(";"):
            if ":" in part:
                k, v = part.split(":", 1)
                k = k.upper()
                if k == "TO":
                    d["address"] = v
                elif k == "SUB":
                    d["subject"] = v
                elif k == "BODY":
                    d["message"] = v
        # Build mailto: URI for opening
        addr = d.get("address", "")
        mailto = f"mailto:{addr}"
        params = []
        if "subject" in d:
            params.append(f"subject={d['subject']}")
        if "message" in d:
            params.append(f"body={d['message']}")
        if params:
            mailto += "?" + "&".join(params)
        d["url"] = mailto
        return QrResult(QrType.EMAIL, stripped, _("E-mail"), d)

    # Wi-Fi
    if lower.startswith("wifi:"):
        d = {}
        content = stripped[5:]
        if content.endswith(";;"):
            content = content[:-1]
        for part in content.split(";"):
            if ":" in part:
                k, v = part.split(":", 1)
                k = k.upper()
                if k == "S":
                    d["ssid"] = v
                elif k == "P":
                    d["password"] = v
                elif k == "T":
                    d["security"] = v
                elif k == "H":
                    d["hidden"] = v
        return QrResult(QrType.WIFI, stripped, _("Wi-Fi"), d)

    # Geolocation
    if lower.startswith("geo:"):
        coords = stripped[4:].split("?")[0]
        parts = coords.split(",")
        d = {"coordinates": coords}
        if len(parts) >= 2:
            d["latitude"] = parts[0]
            d["longitude"] = parts[1]
        return QrResult(QrType.GEO, stripped, _("Location"), d)

    # Calendar event
    if "BEGIN:VEVENT" in stripped:
        d = {}
        for line in stripped.splitlines():
            if line.startswith("SUMMARY:"):
                d["summary"] = line[8:]
            elif line.startswith("DTSTART"):
                d["start"] = line.split(":", 1)[-1] if ":" in line else ""
            elif line.startswith("DTEND"):
                d["end"] = line.split(":", 1)[-1] if ":" in line else ""
            elif line.startswith("LOCATION:"):
                d["location"] = line[9:]
        return QrResult(QrType.CALENDAR, stripped, _("Calendar Event"), d)

    # MECARD (treat as contact card, like vCard)
    if lower.startswith("mecard:"):
        d = {}
        content = stripped[7:]
        # Remove trailing ;;
        if content.endswith(";;"):
            content = content[:-1]
        for part in content.split(";"):
            if ":" in part:
                k, v = part.split(":", 1)
                k = k.upper()
                if k == "N":
                    # MECARD uses Last,First — rearrange to First Last
                    parts_name = v.split(",")
                    if len(parts_name) == 2:
                        d["name"] = f"{parts_name[1].strip()} {parts_name[0].strip()}"
                    else:
                        d["name"] = v
                elif k == "TEL":
                    d["phone"] = v
                elif k == "EMAIL":
                    d["email"] = v
                elif k == "ORG":
                    d["org"] = v
                elif k == "URL":
                    d["url"] = v
                elif k == "ADR":
                    d["address"] = v.replace(",", " ").strip()
                elif k == "NOTE":
                    d["note"] = v
        return QrResult(QrType.VCARD, stripped, _("Contact Card"), d)

    # vCard
    if "BEGIN:VCARD" in stripped:
        d = {}
        for line in stripped.splitlines():
            if line.startswith("FN:"):
                d["name"] = line[3:]
            elif line.startswith("TEL") and ":" in line:
                d["phone"] = line.split(":", 1)[1]
            elif line.startswith("EMAIL") and ":" in line:
                d["email"] = line.split(":", 1)[1]
            elif line.startswith("ORG:"):
                d["org"] = line[4:]
            elif line.startswith("TITLE:"):
                d["title"] = line[6:]
            elif line.startswith("URL:"):
                d["url"] = line[4:]
            elif line.startswith("ADR") and ":" in line:
                d["address"] = line.split(":", 1)[1].replace(";", " ").strip()
        return QrResult(QrType.VCARD, stripped, _("Contact Card"), d)

    # PIX / EMV payment
    if lower.startswith("000201") or lower.startswith("pix:"):
        return QrResult(QrType.PAYMENT_PIX, stripped, _("Payment (PIX)"),
                        {"payload": stripped})

    # Crypto
    for prefix in ("bitcoin:", "ethereum:", "litecoin:", "dogecoin:"):
        if lower.startswith(prefix):
            addr = stripped.split(":", 1)[1].split("?")[0]
            coin = prefix.rstrip(":")
            return QrResult(QrType.CRYPTO, stripped, coin.capitalize(),
                            {"address": addr, "coin": coin})

    # TOTP / OTP auth
    if lower.startswith("otpauth://"):
        d = {"uri": stripped}
        m = re.search(r"otpauth://totp/(.+?)\?", stripped)
        if m:
            d["account"] = m.group(1)
        m = re.search(r"secret=([^&]+)", stripped)
        if m:
            d["secret"] = m.group(1)
        m = re.search(r"issuer=([^&]+)", stripped)
        if m:
            d["issuer"] = m.group(1)
        return QrResult(QrType.TOTP, stripped, _("Authentication (TOTP)"), d)

    # URL-based types
    if lower.startswith("http://") or lower.startswith("https://"):
        from urllib.parse import urlparse
        parsed = urlparse(stripped)
        host = parsed.hostname or ""

        # App store
        for domain in _APP_STORE_DOMAINS:
            if host.endswith(domain):
                return QrResult(QrType.APP_STORE, stripped, _("App Store"), {"url": stripped})

        # Social network
        for domain in _SOCIAL_DOMAINS:
            if host.endswith(domain):
                return QrResult(QrType.SOCIAL, stripped, _("Social Network"), {"url": stripped})

        # Generic URL
        return QrResult(QrType.URL, stripped, _("URL"), {"url": stripped})

    # Fallback: plain text
    return QrResult(QrType.TEXT, stripped, _("Text"), {"text": stripped})


# -- Type icons and labels ---

_TYPE_ICONS = {
    QrType.URL: "web-browser-symbolic",
    QrType.VCARD: "contact-new-symbolic",
    QrType.TEXT: "text-x-generic-symbolic",
    QrType.PHONE: "call-start-symbolic",
    QrType.SMS: "mail-send-symbolic",
    QrType.EMAIL: "mail-unread-symbolic",
    QrType.WIFI: "network-wireless-symbolic",
    QrType.GEO: "find-location-symbolic",
    QrType.CALENDAR: "x-office-calendar-symbolic",
    QrType.APP_STORE: "system-software-install-symbolic",
    QrType.PAYMENT_PIX: "tag-symbolic",
    QrType.CRYPTO: "emblem-money-symbolic",
    QrType.SOCIAL: "system-users-symbolic",
    QrType.TOTP: "channel-secure-symbolic",
}


class QrDialog(Adw.Window):
    """Standalone window showing parsed QR code result with contextual actions."""

    __gtype_name__ = "QrDialog"

    def __init__(self, qr_result: QrResult, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._result = qr_result
        self.set_title(_("QR Code Detected"))
        self.set_default_size(420, 480)
        self.set_modal(True)
        self.set_resizable(True)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        scroll = Gtk.ScrolledWindow(vexpand=True)
        clamp = Adw.Clamp(maximum_size=400, tightening_threshold=360)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        # Type icon + title (compact)
        icon_name = _TYPE_ICONS.get(qr_result.qr_type, "dialog-information-symbolic")
        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        title_box.set_halign(Gtk.Align.CENTER)
        title_box.set_margin_bottom(4)
        title_icon = Gtk.Image(icon_name=icon_name, pixel_size=32)
        title_label = Gtk.Label(label=qr_result.title, css_classes=["title-2"])
        title_box.append(title_icon)
        title_box.append(title_label)
        box.append(title_box)

        # Details group
        details_group = Adw.PreferencesGroup()
        box.append(details_group)
        self._build_details(details_group, qr_result)

        # Action buttons (FlowBox for side-by-side layout)
        actions_group = Adw.PreferencesGroup(title=_("Actions"))
        btn_flow = Gtk.FlowBox(
            selection_mode=Gtk.SelectionMode.NONE,
            homogeneous=True,
            max_children_per_line=2,
            min_children_per_line=2,
            column_spacing=8,
            row_spacing=8,
        )
        btn_flow.set_margin_top(8)
        self._build_actions(btn_flow, qr_result)
        actions_group.add(btn_flow)
        box.append(actions_group)

        # Status feedback label
        self._status_label = Gtk.Label(
            label="", xalign=0.5,
            css_classes=["dim-label"],
        )
        self._status_label.set_margin_top(4)
        box.append(self._status_label)

        clamp.set_child(box)
        scroll.set_child(clamp)
        toolbar.set_content(scroll)
        self.set_content(toolbar)

    def _build_details(self, group: Adw.PreferencesGroup, qr: QrResult) -> None:
        """Add rows for each detail field."""
        field_labels = {
            "url": _("URL"),
            "text": _("Text"),
            "number": _("Number"),
            "message": _("Message"),
            "address": _("Address"),
            "name": _("Name"),
            "phone": _("Phone"),
            "email": _("E-mail"),
            "org": _("Organization"),
            "title": _("Title"),
            "ssid": _("Network (SSID)"),
            "password": _("Password"),
            "security": _("Security"),
            "hidden": _("Hidden"),
            "latitude": _("Latitude"),
            "longitude": _("Longitude"),
            "coordinates": _("Coordinates"),
            "summary": _("Summary"),
            "start": _("Start"),
            "end": _("End"),
            "location": _("Location"),
            "payload": _("Payload"),
            "coin": _("Currency"),
            "account": _("Account"),
            "secret": _("Secret"),
            "issuer": _("Issuer"),
            "uri": _("URI"),
            "subject": _("Subject"),
        }

        for key, value in qr.details.items():
            label = field_labels.get(key, key.capitalize())
            row = Adw.ActionRow(title=label, subtitle=value)
            row.set_subtitle_selectable(True)
            # Copy button for each field
            copy_btn = Gtk.Button(icon_name="edit-copy-symbolic")
            copy_btn.add_css_class("flat")
            copy_btn.set_valign(Gtk.Align.CENTER)
            copy_btn.set_tooltip_text(_("Copy"))
            copy_btn.connect("clicked", self._make_copy_handler(value))
            row.add_suffix(copy_btn)
            group.add(row)

    def _build_actions(self, flow: Gtk.FlowBox, qr: QrResult) -> None:
        """Add contextual action buttons based on QR type."""
        t = qr.qr_type

        if t == QrType.URL or t == QrType.SOCIAL or t == QrType.APP_STORE:
            self._add_btn(flow, _("Open in Browser"), "web-browser-symbolic",
                          self._open_url, "suggested-action")

        elif t == QrType.PHONE:
            self._add_btn(flow, _("Copy Number"), "edit-copy-symbolic",
                          lambda _b: self._copy(qr.details.get("number", "")))

        elif t == QrType.SMS:
            self._add_btn(flow, _("Copy Number"), "edit-copy-symbolic",
                          lambda _b: self._copy(qr.details.get("number", "")))
            if qr.details.get("message"):
                self._add_btn(flow, _("Copy Message"), "edit-copy-symbolic",
                              lambda _b: self._copy(qr.details.get("message", "")))
            self._add_btn(flow, _("WhatsApp"), "send-to-symbolic",
                          self._open_whatsapp)

        elif t == QrType.EMAIL:
            self._add_btn(flow, _("Open E-mail Client"), "mail-send-symbolic",
                          self._open_url, "suggested-action")

        elif t == QrType.WIFI:
            if qr.details.get("password"):
                self._add_btn(flow, _("Copy Password"), "edit-copy-symbolic",
                              lambda _b: self._copy(qr.details.get("password", "")),
                              "suggested-action")
            if qr.details.get("ssid"):
                self._add_btn(flow, _("Copy SSID"), "edit-copy-symbolic",
                              lambda _b: self._copy(qr.details.get("ssid", "")))

        elif t == QrType.GEO:
            self._add_btn(flow, _("Open in Maps"), "find-location-symbolic",
                          self._open_geo, "suggested-action")

        elif t == QrType.CALENDAR:
            self._add_btn(flow, _("Save .ics File"), "document-save-symbolic",
                          self._save_calendar, "suggested-action")

        elif t == QrType.VCARD:
            self._add_btn(flow, _("Save Contact"), "contact-new-symbolic",
                          self._save_vcard, "suggested-action")

        elif t == QrType.PAYMENT_PIX:
            self._add_btn(flow, _("Copy PIX"), "edit-copy-symbolic",
                          lambda _b: self._copy(qr.details.get("payload", "")),
                          "suggested-action")

        elif t == QrType.CRYPTO:
            self._add_btn(flow, _("Copy Address"), "edit-copy-symbolic",
                          lambda _b: self._copy(qr.details.get("address", "")),
                          "suggested-action")

        elif t == QrType.TOTP:
            self._add_btn(flow, _("Copy Secret"), "edit-copy-symbolic",
                          lambda _b: self._copy(qr.details.get("secret", "")),
                          "suggested-action")
            self._add_btn(flow, _("Copy URI"), "edit-copy-symbolic",
                          lambda _b: self._copy(qr.details.get("uri", "")))

        # Always add "Copy Raw" and "Save" at the end
        self._add_btn(flow, _("Copy Raw"), "edit-copy-symbolic",
                      lambda _b: self._copy(qr.raw))
        self._add_btn(flow, _("Save to File"), "document-save-symbolic",
                      self._save_to_file)

    def _add_btn(self, flow: Gtk.FlowBox, label: str, icon: str,
                 callback: Any, css: str | None = None) -> None:
        btn = Gtk.Button()
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.CENTER)
        btn_box.append(Gtk.Image(icon_name=icon))
        btn_box.append(Gtk.Label(label=label))
        btn.set_child(btn_box)
        if css:
            btn.add_css_class(css)
        btn.connect("clicked", callback)
        flow.insert(btn, -1)

    # -- actions ---

    def _make_copy_handler(self, text: str) -> Any:
        def handler(_btn: Gtk.Button) -> None:
            self._copy(text)
        return handler

    def _copy(self, text: str) -> None:
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set(text)
        self._show_status(_("Copied!"))

    def _show_status(self, msg: str) -> None:
        self._status_label.set_text(msg)
        GLib.timeout_add(2000, lambda: self._status_label.set_text("") or False)

    def _open_url(self, _btn: Gtk.Button) -> None:
        url = self._result.details.get("url", self._result.raw)
        Gtk.UriLauncher.new(url).launch(self, None, None, None)

    def _open_geo(self, _btn: Gtk.Button) -> None:
        lat = self._result.details.get("latitude", "0")
        lon = self._result.details.get("longitude", "0")
        url = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=15/{lat}/{lon}"
        Gtk.UriLauncher.new(url).launch(self, None, None, None)

    def _open_whatsapp(self, _btn: Gtk.Button) -> None:
        from urllib.parse import quote
        number = self._result.details.get("number", "").lstrip("+").replace("-", "").replace(" ", "")
        msg = self._result.details.get("message", "")
        url = f"https://wa.me/{number}"
        if msg:
            url += f"?text={quote(msg)}"
        Gtk.UriLauncher.new(url).launch(self, None, None, None)

    def _save_calendar(self, _btn: Gtk.Button) -> None:
        ts = _time.strftime("%Y%m%d_%H%M%S")
        raw = self._result.raw
        if "BEGIN:VCALENDAR" not in raw:
            raw = f"BEGIN:VCALENDAR\nVERSION:2.0\n{raw}\nEND:VCALENDAR"
        self._save_with_dialog(f"event_{ts}.ics", raw, _("iCalendar Files"), "*.ics")

    def _save_vcard(self, _btn: Gtk.Button) -> None:
        name = self._result.details.get("name", "contact")
        safe_name = re.sub(r'[^\w\-]', '_', name)
        raw = self._result.raw
        # Convert MECARD to vCard format if needed
        if raw.strip().upper().startswith("MECARD:"):
            d = self._result.details
            lines = ["BEGIN:VCARD", "VERSION:3.0"]
            if "name" in d:
                lines.append(f"FN:{d['name']}")
            if "phone" in d:
                lines.append(f"TEL:{d['phone']}")
            if "email" in d:
                lines.append(f"EMAIL:{d['email']}")
            if "org" in d:
                lines.append(f"ORG:{d['org']}")
            if "url" in d:
                lines.append(f"URL:{d['url']}")
            if "address" in d:
                lines.append(f"ADR:;;{d['address']};;;;")
            if "note" in d:
                lines.append(f"NOTE:{d['note']}")
            lines.append("END:VCARD")
            raw = "\n".join(lines)
        self._save_with_dialog(f"{safe_name}.vcf", raw, _("vCard Files"), "*.vcf")

    def _save_to_file(self, _btn: Gtk.Button) -> None:
        ts = _time.strftime("%Y%m%d_%H%M%S")
        self._save_with_dialog(f"qrcode_{ts}.txt", self._result.raw,
                              _("Text Files"), "*.txt")

    def _save_with_dialog(self, default_name: str, content: str,
                          filter_name: str, filter_pattern: str) -> None:
        dialog = Gtk.FileDialog()
        dialog.set_initial_name(default_name)

        file_filter = Gtk.FileFilter()
        file_filter.set_name(filter_name)
        file_filter.add_pattern(filter_pattern)
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(file_filter)
        all_filter = Gtk.FileFilter()
        all_filter.set_name(_("All Files"))
        all_filter.add_pattern("*")
        filters.append(all_filter)
        dialog.set_filters(filters)
        dialog.set_default_filter(file_filter)

        dialog.save(self, None, self._on_save_response, content)

    def _on_save_response(self, dialog: Gtk.FileDialog,
                          result: Gio.AsyncResult, content: str) -> None:
        try:
            gfile = dialog.save_finish(result)
            path = gfile.get_path()
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            self._show_status(_("Saved: %s") % os.path.basename(path))
        except GLib.Error:
            pass  # user cancelled
        except Exception as e:
            self._show_status(_("Error: %s") % str(e))
