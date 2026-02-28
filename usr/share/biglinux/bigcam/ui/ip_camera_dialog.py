"""IP camera dialog – add/edit network cameras."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, GObject

from utils.i18n import _


class IPCameraDialog(Adw.Dialog):
    """Dialog for adding a RTSP or HTTP camera URL."""

    __gsignals__ = {
        "camera-added": (GObject.SignalFlags.RUN_LAST, None, (str, str)),
    }

    def __init__(self) -> None:
        super().__init__()
        self.set_title(_("Add IP Camera"))
        self.set_content_width(420)
        self.set_content_height(280)

        page = Adw.ToolbarView()
        header = Adw.HeaderBar()
        page.add_top_bar(header)

        clamp = Adw.Clamp(maximum_size=400, tightening_threshold=300)
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_top=24,
            margin_bottom=24,
            margin_start=16,
            margin_end=16,
        )

        group = Adw.PreferencesGroup(title=_("Camera Details"))

        self._name_row = Adw.EntryRow(title=_("Name"))
        self._name_row.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Camera name")]
        )
        group.add(self._name_row)

        self._url_row = Adw.EntryRow(title=_("URL"))
        self._url_row.set_input_purpose(Gtk.InputPurpose.URL)
        self._url_row.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Camera URL (RTSP or HTTP)")]
        )
        group.add(self._url_row)

        content.append(group)

        # Action button
        add_btn = Gtk.Button(label=_("Add Camera"))
        add_btn.add_css_class("suggested-action")
        add_btn.add_css_class("pill")
        add_btn.set_halign(Gtk.Align.CENTER)
        add_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Add IP camera")]
        )
        add_btn.connect("clicked", self._on_add)
        content.append(add_btn)

        clamp.set_child(content)
        page.set_content(clamp)
        self.set_child(page)

    def _on_add(self, _btn: Gtk.Button) -> None:
        name = self._name_row.get_text().strip()
        url = self._url_row.get_text().strip()
        if not url:
            return
        if not name:
            name = url
        self.emit("camera-added", name, url)
        self.close()
