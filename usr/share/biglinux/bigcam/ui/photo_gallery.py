"""Photo gallery – grid/list view of captured photos with selection support."""

from __future__ import annotations

import os
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, Gdk, GdkPixbuf, GLib

from utils import xdg
from utils.i18n import _


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}" if unit != "B" else f"{nbytes} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def _human_date(timestamp: float) -> str:
    return time.strftime("%d/%m/%Y  %H:%M", time.localtime(timestamp))


class PhotoGallery(Gtk.Box):
    """Gallery of captured photo thumbnails with grid/list and bulk selection."""

    THUMB_SIZE = 160
    LIST_THUMB = 48

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._photos_dir = xdg.photos_dir()
        self._selection_mode = False
        self._selected: set[str] = set()
        self._view = "grid"
        self._items: list[str] = []

        # ── Header ───────────────────────────────────────────────────
        header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin_top=12,
            margin_start=12,
            margin_end=12,
        )
        title = Gtk.Label(label=_("Captured Photos"), hexpand=True, xalign=0)
        title.add_css_class("title-4")
        header.append(title)

        # View toggle (grid / list)
        self._grid_btn = Gtk.ToggleButton(icon_name="view-grid-symbolic")
        self._grid_btn.set_active(True)
        self._grid_btn.set_tooltip_text(_("Grid view"))
        self._list_btn = Gtk.ToggleButton(icon_name="view-list-symbolic")
        self._list_btn.set_group(self._grid_btn)
        self._list_btn.set_tooltip_text(_("List view"))
        self._grid_btn.connect("toggled", self._on_view_toggled, "grid")
        self._list_btn.connect("toggled", self._on_view_toggled, "list")

        view_box = Gtk.Box(spacing=0)
        view_box.add_css_class("linked")
        view_box.append(self._grid_btn)
        view_box.append(self._list_btn)
        header.append(view_box)

        # Select mode toggle
        self._select_btn = Gtk.ToggleButton(icon_name="object-select-symbolic")
        self._select_btn.set_tooltip_text(_("Select items"))
        self._select_btn.connect("toggled", self._on_select_toggled)
        header.append(self._select_btn)

        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.add_css_class("flat")
        refresh_btn.set_tooltip_text(_("Refresh"))
        refresh_btn.connect("clicked", lambda _b: self.refresh())
        header.append(refresh_btn)

        open_btn = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        open_btn.add_css_class("flat")
        open_btn.set_tooltip_text(_("Open photos folder"))
        open_btn.connect("clicked", self._on_open_folder)
        header.append(open_btn)

        self.append(header)

        # ── Content stack (grid / list) ──────────────────────────────
        self._scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            vexpand=True,
        )
        self._stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)

        # Grid
        self._flowbox = Gtk.FlowBox(
            homogeneous=True,
            max_children_per_line=6,
            min_children_per_line=2,
            selection_mode=Gtk.SelectionMode.NONE,
            row_spacing=8,
            column_spacing=8,
            margin_top=8,
            margin_bottom=8,
            margin_start=12,
            margin_end=12,
        )
        self._stack.add_named(self._flowbox, "grid")

        # List
        self._listbox = Gtk.ListBox(
            selection_mode=Gtk.SelectionMode.NONE,
            margin_top=8,
            margin_bottom=8,
            margin_start=12,
            margin_end=12,
        )
        self._listbox.add_css_class("boxed-list")
        self._stack.add_named(self._listbox, "list")

        self._scroll.set_child(self._stack)
        self.append(self._scroll)

        # Empty state
        self._empty = Adw.StatusPage(
            icon_name="image-x-generic-symbolic",
            title=_("No photos yet"),
            description=_("Captured photos will appear here."),
        )
        self._empty.set_visible(False)
        self.append(self._empty)

        # ── Selection action bar ─────────────────────────────────────
        self._action_bar = Gtk.ActionBar()
        self._action_bar.set_visible(False)

        self._sel_label = Gtk.Label(label=_("0 selected"))
        self._action_bar.set_center_widget(self._sel_label)

        select_all_btn = Gtk.Button(label=_("Select All"))
        select_all_btn.connect("clicked", self._on_select_all)
        self._action_bar.pack_start(select_all_btn)

        del_sel_btn = Gtk.Button(label=_("Delete"))
        del_sel_btn.add_css_class("destructive-action")
        del_sel_btn.connect("clicked", self._on_delete_selected)
        self._action_bar.pack_end(del_sel_btn)

        self.append(self._action_bar)

        self.connect("map", self._on_mapped)

    # ── View / selection toggles ─────────────────────────────────────

    def _on_view_toggled(self, btn: Gtk.ToggleButton, mode: str) -> None:
        if not btn.get_active():
            return
        self._view = mode
        self.refresh()

    def _on_select_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._selection_mode = btn.get_active()
        self._selected.clear()
        self._action_bar.set_visible(self._selection_mode)
        self._update_sel_label()
        self.refresh()

    def _update_sel_label(self) -> None:
        n = len(self._selected)
        self._sel_label.set_label(
            _("%d selected") % n if n else _("0 selected")
        )

    # ── Mapped / refresh ─────────────────────────────────────────────

    def _on_mapped(self, _widget: Gtk.Widget) -> None:
        self.refresh()

    def refresh(self) -> None:
        # Clear grid
        child = self._flowbox.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._flowbox.remove(child)
            child = nxt
        # Clear list
        child = self._listbox.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._listbox.remove(child)
            child = nxt

        self._items = self._list_photos()
        has = len(self._items) > 0
        self._empty.set_visible(not has)
        self._scroll.set_visible(has)

        self._stack.set_visible_child_name(self._view)

        for path in self._items[:100]:
            if self._view == "grid":
                w = self._make_grid_item(path)
            else:
                w = self._make_list_item(path)
            if w:
                if self._view == "grid":
                    self._flowbox.append(w)
                else:
                    self._listbox.append(w)

    def _list_photos(self) -> list[str]:
        if not os.path.isdir(self._photos_dir):
            return []
        files: list[str] = []
        for entry in sorted(
            os.scandir(self._photos_dir),
            key=lambda e: e.stat().st_mtime,
            reverse=True,
        ):
            if entry.is_file() and entry.name.lower().endswith(
                (".jpg", ".jpeg", ".png", ".webp")
            ):
                files.append(entry.path)
        return files

    # ── Grid item ────────────────────────────────────────────────────

    def _make_grid_item(self, path: str) -> Gtk.Widget | None:
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

        overlay = Gtk.Overlay()

        if self._selection_mode:
            check = Gtk.CheckButton(active=path in self._selected)
            check.set_halign(Gtk.Align.START)
            check.set_valign(Gtk.Align.START)
            check.set_margin_start(6)
            check.set_margin_top(6)
            check.add_css_class("osd")
            check.connect("toggled", self._on_check_toggled, path)

            btn = Gtk.Button()
            btn.set_child(picture)
            btn.add_css_class("flat")
            btn.connect("clicked", self._on_grid_check_click, check)
            overlay.set_child(btn)
            overlay.add_overlay(check)
        else:
            btn = Gtk.Button()
            btn.set_child(picture)
            btn.add_css_class("flat")
            btn.set_tooltip_text(os.path.basename(path))
            btn.connect("clicked", self._on_open_photo, path)
            overlay.set_child(btn)

            del_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            del_btn.add_css_class("osd")
            del_btn.add_css_class("circular")
            del_btn.add_css_class("delete-thumb-btn")
            del_btn.set_halign(Gtk.Align.END)
            del_btn.set_valign(Gtk.Align.START)
            del_btn.set_margin_end(4)
            del_btn.set_margin_top(4)
            del_btn.set_tooltip_text(_("Delete"))
            del_btn.connect("clicked", self._on_delete_clicked, path)
            overlay.add_overlay(del_btn)

        return overlay

    def _on_grid_check_click(self, _btn: Gtk.Button, check: Gtk.CheckButton) -> None:
        check.set_active(not check.get_active())

    # ── List item ────────────────────────────────────────────────────

    def _make_list_item(self, path: str) -> Gtk.Widget | None:
        name = os.path.basename(path)
        try:
            st = os.stat(path)
            size = _human_size(st.st_size)
            date = _human_date(st.st_mtime)
        except OSError:
            size = ""
            date = ""

        row = Adw.ActionRow(title=name, subtitle=f"{size}  ·  {date}")
        row.set_activatable(not self._selection_mode)

        # Small thumbnail prefix
        try:
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
                path, self.LIST_THUMB, self.LIST_THUMB, True
            )
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            pic = Gtk.Picture.new_for_paintable(texture)
            pic.set_content_fit(Gtk.ContentFit.COVER)
            pic.set_size_request(self.LIST_THUMB, self.LIST_THUMB)
            frame = Gtk.Frame()
            frame.set_child(pic)
            row.add_prefix(frame)
        except Exception:
            icon = Gtk.Image.new_from_icon_name("image-x-generic-symbolic")
            icon.set_pixel_size(self.LIST_THUMB)
            row.add_prefix(icon)

        if self._selection_mode:
            check = Gtk.CheckButton(active=path in self._selected)
            check.set_valign(Gtk.Align.CENTER)
            check.connect("toggled", self._on_check_toggled, path)
            row.add_suffix(check)
            row.connect("activated", lambda _r, c=check: c.set_active(not c.get_active()))
        else:
            row.connect("activated", self._on_row_activated, path)
            del_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            del_btn.add_css_class("flat")
            del_btn.set_valign(Gtk.Align.CENTER)
            del_btn.set_tooltip_text(_("Delete"))
            del_btn.connect("clicked", self._on_delete_clicked, path)
            row.add_suffix(del_btn)

        return row

    def _on_row_activated(self, _row: Adw.ActionRow, path: str) -> None:
        self._on_open_photo(None, path)

    # ── Selection ────────────────────────────────────────────────────

    def _on_check_toggled(self, check: Gtk.CheckButton, path: str) -> None:
        if check.get_active():
            self._selected.add(path)
        else:
            self._selected.discard(path)
        self._update_sel_label()

    def _on_select_all(self, _btn: Gtk.Button) -> None:
        all_selected = len(self._selected) == len(self._items[:100])
        self._selected = set() if all_selected else set(self._items[:100])
        self._update_sel_label()
        self.refresh()

    def _on_delete_selected(self, _btn: Gtk.Button) -> None:
        if not self._selected:
            return
        n = len(self._selected)
        dialog = Adw.AlertDialog(
            heading=_("Delete %d photos?") % n,
            body=_("These photos will be permanently deleted."),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_bulk_delete_response)
        dialog.present(self.get_root())

    def _on_bulk_delete_response(self, _dialog: Adw.AlertDialog, response: str) -> None:
        if response != "delete":
            return
        for p in list(self._selected):
            try:
                os.remove(p)
            except OSError:
                pass
        self._selected.clear()
        self._update_sel_label()
        self.refresh()

    # ── Actions ──────────────────────────────────────────────────────

    def _on_open_photo(self, _btn: Gtk.Button | None, path: str) -> None:
        uri = GLib.filename_to_uri(path)
        Gtk.show_uri(self.get_root(), uri, Gdk.CURRENT_TIME)

    def _on_open_folder(self, _btn: Gtk.Button) -> None:
        os.makedirs(self._photos_dir, exist_ok=True)
        uri = GLib.filename_to_uri(self._photos_dir)
        Gtk.show_uri(self.get_root(), uri, Gdk.CURRENT_TIME)

    def _on_delete_clicked(self, _btn: Gtk.Button, path: str) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Delete photo?"),
            body=_('"%s" will be permanently deleted.') % os.path.basename(path),
        )
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("delete", _("Delete"))
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_delete_response, path)
        dialog.present(self.get_root())

    def _on_delete_response(
        self, _dialog: Adw.AlertDialog, response: str, path: str
    ) -> None:
        if response != "delete":
            return
        try:
            os.remove(path)
        except OSError:
            pass
        self.refresh()
        self.refresh()
