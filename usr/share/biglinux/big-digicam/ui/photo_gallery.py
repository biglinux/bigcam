"""Photo gallery – thumbnails grid of captured photos."""

from __future__ import annotations

import os
import subprocess

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, Gdk, Gio, GdkPixbuf, GLib

from utils import xdg
from utils.i18n import _


class PhotoGallery(Gtk.ScrolledWindow):
    """Grid of captured photo thumbnails."""

    THUMB_SIZE = 160

    def __init__(self) -> None:
        super().__init__(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        self._photos_dir = xdg.photos_dir()

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                       margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)

        # Header toolbar
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label=_("Captured Photos"), hexpand=True, xalign=0)
        title.add_css_class("title-4")

        open_btn = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        open_btn.add_css_class("flat")
        open_btn.set_tooltip_text(_("Open photos folder"))
        open_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Open photos folder")]
        )
        open_btn.connect("clicked", self._on_open_folder)

        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.add_css_class("flat")
        refresh_btn.set_tooltip_text(_("Refresh"))
        refresh_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Refresh photo gallery")]
        )
        refresh_btn.connect("clicked", lambda _b: self.refresh())

        header.append(title)
        header.append(refresh_btn)
        header.append(open_btn)
        vbox.append(header)

        # FlowBox for thumbnails
        self._flowbox = Gtk.FlowBox(
            homogeneous=True,
            max_children_per_line=6,
            min_children_per_line=2,
            selection_mode=Gtk.SelectionMode.NONE,
            row_spacing=8,
            column_spacing=8,
        )
        vbox.append(self._flowbox)

        # Empty state
        self._empty = Adw.StatusPage(
            icon_name="image-missing-symbolic",
            title=_("No photos yet"),
            description=_("Captured photos will appear here."),
        )
        self._empty.set_visible(False)
        vbox.append(self._empty)

        self.set_child(vbox)
        self.connect("map", self._on_mapped)

    def _on_mapped(self, _widget: Gtk.Widget) -> None:
        self.refresh()

    def refresh(self) -> None:
        """Scan photos directory and rebuild the grid."""
        # Remove old children
        child = self._flowbox.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self._flowbox.remove(child)
            child = next_child

        photos = self._list_photos()
        self._empty.set_visible(len(photos) == 0)

        for path in photos[:100]:  # limit for performance
            thumb = self._make_thumbnail(path)
            if thumb:
                self._flowbox.append(thumb)

    def _list_photos(self) -> list[str]:
        if not os.path.isdir(self._photos_dir):
            return []
        files: list[str] = []
        for entry in sorted(os.scandir(self._photos_dir), key=lambda e: e.stat().st_mtime, reverse=True):
            if entry.is_file() and entry.name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                files.append(entry.path)
        return files

    def _make_thumbnail(self, path: str) -> Gtk.Widget | None:
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                path, self.THUMB_SIZE, self.THUMB_SIZE, True
            )
        except Exception:
            return None

        texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        picture = Gtk.Picture.new_for_paintable(texture)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_size_request(self.THUMB_SIZE, self.THUMB_SIZE)
        picture.add_css_class("card")

        btn = Gtk.Button()
        btn.set_child(picture)
        btn.add_css_class("flat")
        btn.set_tooltip_text(os.path.basename(path))
        btn.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Open %s") % os.path.basename(path)],
        )
        btn.connect("clicked", self._on_open_photo, path)
        return btn

    def _on_open_photo(self, _btn: Gtk.Button, path: str) -> None:
        """Open photo in default system viewer."""
        uri = GLib.filename_to_uri(path)
        Gtk.show_uri(self.get_root(), uri, Gdk.CURRENT_TIME)

    def _on_open_folder(self, _btn: Gtk.Button) -> None:
        os.makedirs(self._photos_dir, exist_ok=True)
        uri = GLib.filename_to_uri(self._photos_dir)
        Gtk.show_uri(self.get_root(), uri, Gdk.CURRENT_TIME)
