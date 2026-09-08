"""Shared, paginated gallery with bounded asynchronous metadata and reversible trash."""
from collections import deque
import os
import time
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk
from core.media_library import scan, thumbnail, duration, PHOTO_EXTS, VIDEO_EXTS
from utils.async_worker import run_async
from utils.settings_manager import SettingsManager
from utils.i18n import _, ngettext


class MediaGallery(Gtk.Box):
    PAGE_SIZE = 100

    def __init__(self, kind, directory):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._kind = kind
        self._directory = directory
        self._settings = SettingsManager()
        self._view = self._settings.get(f"gallery-{kind}-view", "grid")
        self._closed = False
        self._generation = 0
        self._limit = self.PAGE_SIZE
        self._selected = set()
        self._entries = []
        self._selection_mode = False
        self._scope = {"alive": False}
        header = Gtk.Box(spacing=6, margin_top=12, margin_start=12, margin_end=12)
        title = Gtk.Label(label=_("Captured Photos") if kind == "photo" else _("Recorded Videos"),
                          hexpand=True, xalign=0, wrap=True)
        title.add_css_class("heading")
        header.append(title)
        for view, icon, label in [("grid", "view-grid-symbolic", _("Grid view")),
                                 ("list", "view-list-symbolic", _("List view"))]:
            button = Gtk.Button(icon_name=icon, tooltip_text=label)
            button.update_property([Gtk.AccessibleProperty.LABEL], [label])
            button.connect("clicked", lambda _button, target=view: self._set_view(target))
            header.append(button)
        selection = Gtk.ToggleButton(icon_name="object-select-symbolic", tooltip_text=_("Select items"))
        selection.update_property([Gtk.AccessibleProperty.LABEL], [_("Select items")])
        selection.connect("toggled", self._toggle_selection)
        header.append(selection)
        self.append(header)
        self._status = Gtk.Label(wrap=True, margin_start=12, margin_end=12)
        self._status.update_property([Gtk.AccessibleProperty.LABEL], [_("Gallery status")])
        self.append(self._status)
        self._stack = Gtk.Stack(vexpand=True)
        self._grid = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                 min_children_per_line=1, max_children_per_line=6, column_spacing=8, row_spacing=8)
        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        self._stack.add_named(self._grid, "grid")
        self._stack.add_named(self._list, "list")
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(self._stack)
        self.append(scroll)
        actions = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_bottom=12)
        self._more = Gtk.Button(label=_("Load more"))
        self._more.connect("clicked", self._load_more)
        actions.append(self._more)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Refresh"))
        refresh.update_property([Gtk.AccessibleProperty.LABEL], [_("Refresh gallery")])
        refresh.connect("clicked", lambda _button: self.refresh())
        actions.append(refresh)
        folder = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text=_("Open folder"))
        folder.update_property([Gtk.AccessibleProperty.LABEL], [_("Open media folder")])
        folder.connect("clicked", lambda _button: self._open(self._directory))
        actions.append(folder)
        self.append(actions)
        self._selection_bar = Gtk.Box(spacing=6, visible=False, margin_start=12, margin_end=12, margin_bottom=12)
        self._select_all = Gtk.Button(label=_("Select displayed items"))
        self._select_all.connect("clicked", self._on_select_all)
        self._selection_bar.append(self._select_all)
        trash = Gtk.Button(label=_("Move to Trash"))
        trash.add_css_class("destructive-action")
        trash.connect("clicked", lambda _button: self._confirm_trash(list(self._selected)))
        self._selection_bar.append(trash)
        self.append(self._selection_bar)
        self.connect("map", lambda _widget: self.refresh())

    def _set_view(self, view):
        self._view = view
        self._settings.set(f"gallery-{self._kind}-view", view)
        self._rebuild()

    def _toggle_selection(self, button):
        self._selection_mode = button.get_active()
        self._selected.clear()
        self._selection_bar.set_visible(self._selection_mode)
        self._rebuild()

    def _load_more(self, _button):
        self._limit += self.PAGE_SIZE
        self._rebuild()

    def refresh(self):
        if self._closed:
            return
        self._generation += 1
        generation = self._generation
        self._status.set_label(_("Loading media…"))
        extensions = PHOTO_EXTS if self._kind == "photo" else VIDEO_EXTS
        def done(entries):
            if self._closed or generation != self._generation:
                return
            self._entries = entries
            self._selected.intersection_update(entry.path for entry in entries)
            self._rebuild()
        run_async(lambda: scan(self._directory, extensions), on_success=done,
                  on_error=lambda exc: self._error(_("Could not read the media folder.")))

    def _rebuild(self):
        if self._closed:
            return
        self._scope["alive"] = False
        scope = self._scope = {"alive": True, "pending": deque(), "active": 0}
        for container in (self._grid, self._list):
            child = container.get_first_child()
            while child:
                following = child.get_next_sibling()
                container.remove(child)
                child = following
        self._stack.set_visible_child_name("list" if self._view == "list" else "grid")
        for entry in self._entries[:self._limit]:
            picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN)
            picture.set_size_request(48 if self._view == "list" else 160, 48 if self._view == "list" else 160)
            picture.set_alternative_text(entry.name)
            if self._view == "list":
                row = Adw.ActionRow(title=entry.name, subtitle=f"{GLib.format_size(entry.size)} · {time.strftime('%x %X', time.localtime(entry.modified))}")
                row.add_prefix(picture)
                row.set_activatable(True)
                row.connect("activated", lambda _row, path=entry.path: self._activate(path))
                self._list.append(row)
                suffix = row.add_suffix
            else:
                row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                button = Gtk.Button(child=picture, tooltip_text=entry.name)
                button.update_property([Gtk.AccessibleProperty.LABEL], [_("Open %s") % entry.name])
                button.connect("clicked", lambda _button, path=entry.path: self._activate(path))
                row.append(button)
                self._grid.append(row)
                suffix = row.append
            if self._selection_mode:
                check = Gtk.CheckButton(label=_("Select"), active=entry.path in self._selected)
                check.update_property([Gtk.AccessibleProperty.LABEL], [_("Select %s") % entry.name])
                check.connect("toggled", self._select, entry.path)
                suffix(check)
            else:
                trash = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, tooltip_text=_("Move to Trash"))
                trash.update_property([Gtk.AccessibleProperty.LABEL], [_("Move %s to Trash") % entry.name])
                trash.connect("clicked", lambda _button, path=entry.path: self._confirm_trash([path]))
                suffix(trash)
            scope["pending"].append((entry, picture))
        self._more.set_visible(len(self._entries) > self._limit)
        self._update_count()
        self._pump(scope)

    def _pump(self, scope):
        while scope["alive"] and scope["pending"] and scope["active"] < 2:
            entry, picture = scope["pending"].popleft()
            scope["active"] += 1
            def loaded(path, image=picture, current=scope):
                current["active"] -= 1
                if current["alive"] and not self._closed:
                    if path:
                        try:
                            image.set_filename(path)
                        except GLib.Error:
                            pass
                    self._pump(current)
            run_async(lambda item=entry: thumbnail(item), on_success=loaded,
                      on_error=lambda exc, callback=loaded: callback(None))

    def _activate(self, path):
        if self._selection_mode:
            if path in self._selected:
                self._selected.remove(path)
            else:
                self._selected.add(path)
            self._rebuild()
        else:
            self._open(path)

    def _select(self, check, path):
        if check.get_active():
            self._selected.add(path)
        else:
            self._selected.discard(path)
        self._update_count()

    def _on_select_all(self, _button):
        visible = {entry.path for entry in self._entries[:self._limit]}
        self._selected = set() if visible <= self._selected else visible
        self._rebuild()

    def _update_count(self):
        count = len(self._selected)
        if self._selection_mode:
            self._status.set_label(ngettext("%d item selected", "%d items selected", count) % count)
        elif self._entries:
            self._status.set_label(_("Showing %(shown)d of %(total)d items") %
                                   {"shown": min(self._limit, len(self._entries)), "total": len(self._entries)})
        else:
            self._status.set_label(_("No media yet"))

    def _open(self, path):
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(path))
        def done(launcher, result):
            try:
                launcher.launch_finish(result)
            except GLib.Error:
                self._error(_("Could not open the file or folder."))
        launcher.launch(self.get_root(), None, done)

    def _confirm_trash(self, paths):
        allowed = {entry.path for entry in self._entries}
        paths = [path for path in paths if path in allowed]
        if not paths:
            return
        count = len(paths)
        dialog = Adw.AlertDialog(heading=ngettext("Move %d item to Trash?", "Move %d items to Trash?", count) % count,
                                 body=_("You can restore these files from the system Trash. Files that cannot be trashed will be kept."))
        dialog.add_response("cancel", _("Cancel"))
        dialog.add_response("trash", _("Move to Trash"))
        dialog.set_close_response("cancel")
        dialog.set_default_response("cancel")
        dialog.set_response_appearance("trash", Adw.ResponseAppearance.DESTRUCTIVE)
        def response(_dialog, choice):
            if choice != "trash":
                return
            def move():
                failed = []
                for path in paths:
                    try:
                        Gio.File.new_for_path(path).trash(None)
                    except GLib.Error:
                        failed.append(path)
                return failed
            def done(failed):
                self._selected.difference_update(set(paths) - set(failed))
                self.refresh()
                if failed:
                    self._error(ngettext("%d file could not be moved to Trash.", "%d files could not be moved to Trash.", len(failed)) % len(failed))
            run_async(move, on_success=done, on_error=lambda exc: self._error(_("Could not move files to Trash.")))
        dialog.connect("response", response)
        dialog.present(self.get_root())

    def _error(self, message):
        if self._closed:
            return
        self._status.set_label(message)
        root = self.get_root()
        if root and hasattr(root, "_show_notification"):
            root._show_notification(message, "error", 0)

    def cleanup(self):
        self._closed = True
        self._generation += 1
        self._scope["alive"] = False
        self._scope.get("pending", deque()).clear()
