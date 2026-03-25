"""Video gallery – grid/list view of recorded videos with selection support."""

from __future__ import annotations

import logging
import os
import subprocess
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, Gdk, GdkPixbuf, GLib

from utils import xdg
from utils.async_worker import run_async
from utils.i18n import _

log = logging.getLogger(__name__)


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}" if unit != "B" else f"{nbytes} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def _human_date(timestamp: float) -> str:
    return time.strftime("%d/%m/%Y  %H:%M", time.localtime(timestamp))


class _VideoMeta:
    __slots__ = ("path", "name", "size", "mtime", "duration", "thumb_path")

    def __init__(self, path: str) -> None:
        self.path = path
        self.name = os.path.basename(path)
        try:
            st = os.stat(path)
            self.size = st.st_size
            self.mtime = st.st_mtime
        except OSError:
            self.size = 0
            self.mtime = 0.0
        self.duration: str | None = None
        self.thumb_path: str | None = None


class VideoGallery(Gtk.Box):
    """Gallery of recorded video thumbnails with grid/list and bulk selection."""

    THUMB_SIZE = 160
    LIST_THUMB = 48

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._videos_dir = xdg.videos_dir()
        self._selection_mode = False
        self._selected: set[str] = set()
        self._view = "grid"
        self._metas: list[_VideoMeta] = []

        # ── Header ───────────────────────────────────────────────────
        header = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin_top=12,
            margin_start=12,
            margin_end=12,
        )
        title = Gtk.Label(label=_("Recorded Videos"), hexpand=True, xalign=0)
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
        open_btn.set_tooltip_text(_("Open videos folder"))
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
            icon_name="video-display-symbolic",
            title=_("No videos yet"),
            description=_("Recorded videos will appear here."),
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
        self._rebuild_from_cache()

    def _on_select_toggled(self, btn: Gtk.ToggleButton) -> None:
        self._selection_mode = btn.get_active()
        self._selected.clear()
        self._action_bar.set_visible(self._selection_mode)
        self._update_sel_label()
        self._rebuild_from_cache()

    def _update_sel_label(self) -> None:
        n = len(self._selected)
        self._sel_label.set_label(
            _("%d selected") % n if n else _("0 selected")
        )

    # ── Mapped / refresh ─────────────────────────────────────────────

    def _on_mapped(self, _widget: Gtk.Widget) -> None:
        self.refresh()

    def refresh(self) -> None:
        self._clear_containers()
        videos = self._list_videos()
        self._empty.set_visible(len(videos) == 0)
        self._scroll.set_visible(len(videos) > 0)
        if not videos:
            self._metas = []
            return

        batch = videos[:100]

        def _prepare() -> list[_VideoMeta]:
            results = []
            for path in batch:
                m = _VideoMeta(path)
                m.thumb_path = self._get_thumb_path(path)
                if m.thumb_path and not os.path.isfile(m.thumb_path):
                    self._generate_thumb_file(path, m.thumb_path)
                m.duration = self._get_duration(path)
                results.append(m)
            return results

        def _done(metas: list[_VideoMeta]) -> None:
            self._metas = metas
            self._rebuild_from_cache()

        run_async(_prepare, on_success=_done)

    def _rebuild_from_cache(self) -> None:
        self._clear_containers()
        self._stack.set_visible_child_name(self._view)
        self._empty.set_visible(len(self._metas) == 0)
        self._scroll.set_visible(len(self._metas) > 0)

        for m in self._metas:
            if self._view == "grid":
                w = self._make_grid_item(m)
            else:
                w = self._make_list_item(m)
            if w:
                if self._view == "grid":
                    self._flowbox.append(w)
                else:
                    self._listbox.append(w)

    def _clear_containers(self) -> None:
        for container in (self._flowbox, self._listbox):
            child = container.get_first_child()
            while child:
                nxt = child.get_next_sibling()
                container.remove(child)
                child = nxt

    def _list_videos(self) -> list[str]:
        if not os.path.isdir(self._videos_dir):
            return []
        exts = (".mkv", ".mp4", ".webm", ".avi", ".mov")
        files: list[str] = []
        for entry in sorted(
            os.scandir(self._videos_dir),
            key=lambda e: e.stat().st_mtime,
            reverse=True,
        ):
            if entry.is_file() and entry.name.lower().endswith(exts):
                files.append(entry.path)
        return files

    # ── Grid item ────────────────────────────────────────────────────

    def _make_grid_item(self, m: _VideoMeta) -> Gtk.Widget | None:
        pixbuf = self._load_pixbuf(m.thumb_path, self.THUMB_SIZE) if m.thumb_path else None

        if pixbuf:
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            picture = Gtk.Picture.new_for_paintable(texture)
            picture.set_content_fit(Gtk.ContentFit.COVER)
        else:
            picture = Gtk.Image.new_from_icon_name("video-x-generic-symbolic")
            picture.set_pixel_size(48)
        picture.set_size_request(self.THUMB_SIZE, self.THUMB_SIZE)
        picture.add_css_class("card")

        # Inner overlay: play icon + duration
        inner = Gtk.Overlay()
        inner.set_child(picture)

        play_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        play_icon.set_pixel_size(32)
        play_icon.set_opacity(0.8)
        play_icon.set_halign(Gtk.Align.CENTER)
        play_icon.set_valign(Gtk.Align.CENTER)
        inner.add_overlay(play_icon)

        if m.duration:
            dur_label = Gtk.Label(label=m.duration)
            dur_label.add_css_class("caption")
            dur_box = Gtk.Box()
            dur_box.append(dur_label)
            dur_box.add_css_class("osd")
            dur_box.set_halign(Gtk.Align.END)
            dur_box.set_valign(Gtk.Align.END)
            dur_box.set_margin_end(4)
            dur_box.set_margin_bottom(4)
            inner.add_overlay(dur_box)

        # Outer overlay
        outer = Gtk.Overlay()

        if self._selection_mode:
            check = Gtk.CheckButton(active=m.path in self._selected)
            check.set_halign(Gtk.Align.START)
            check.set_valign(Gtk.Align.START)
            check.set_margin_start(6)
            check.set_margin_top(6)
            check.add_css_class("osd")
            check.connect("toggled", self._on_check_toggled, m.path)

            btn = Gtk.Button()
            btn.set_child(inner)
            btn.add_css_class("flat")
            btn.connect("clicked", self._on_grid_check_click, check)
            outer.set_child(btn)
            outer.add_overlay(check)
        else:
            btn = Gtk.Button()
            btn.set_child(inner)
            btn.add_css_class("flat")
            btn.set_tooltip_text(m.name)
            btn.connect("clicked", self._on_open_video, m.path)
            outer.set_child(btn)

            del_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            del_btn.add_css_class("osd")
            del_btn.add_css_class("circular")
            del_btn.add_css_class("delete-thumb-btn")
            del_btn.set_halign(Gtk.Align.END)
            del_btn.set_valign(Gtk.Align.START)
            del_btn.set_margin_end(4)
            del_btn.set_margin_top(4)
            del_btn.set_tooltip_text(_("Delete"))
            del_btn.connect("clicked", self._on_delete_clicked, m.path)
            outer.add_overlay(del_btn)

        return outer

    def _on_grid_check_click(self, _btn: Gtk.Button, check: Gtk.CheckButton) -> None:
        check.set_active(not check.get_active())

    # ── List item ────────────────────────────────────────────────────

    def _make_list_item(self, m: _VideoMeta) -> Gtk.Widget | None:
        parts = []
        if m.duration:
            parts.append(m.duration)
        if m.size:
            parts.append(_human_size(m.size))
        if m.mtime:
            parts.append(_human_date(m.mtime))

        row = Adw.ActionRow(title=m.name, subtitle="  ·  ".join(parts))
        row.set_activatable(not self._selection_mode)

        # Small thumbnail prefix
        pixbuf = self._load_pixbuf(m.thumb_path, self.LIST_THUMB) if m.thumb_path else None
        if pixbuf:
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            pic = Gtk.Picture.new_for_paintable(texture)
            pic.set_content_fit(Gtk.ContentFit.COVER)
            pic.set_size_request(self.LIST_THUMB, self.LIST_THUMB)
            frame = Gtk.Frame()
            frame.set_child(pic)
            row.add_prefix(frame)
        else:
            icon = Gtk.Image.new_from_icon_name("video-x-generic-symbolic")
            icon.set_pixel_size(self.LIST_THUMB)
            row.add_prefix(icon)

        if self._selection_mode:
            check = Gtk.CheckButton(active=m.path in self._selected)
            check.set_valign(Gtk.Align.CENTER)
            check.connect("toggled", self._on_check_toggled, m.path)
            row.add_suffix(check)
            row.connect("activated", lambda _r, c=check: c.set_active(not c.get_active()))
        else:
            row.connect("activated", self._on_row_activated, m.path)
            del_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
            del_btn.add_css_class("flat")
            del_btn.set_valign(Gtk.Align.CENTER)
            del_btn.set_tooltip_text(_("Delete"))
            del_btn.connect("clicked", self._on_delete_clicked, m.path)
            row.add_suffix(del_btn)

        return row

    def _on_row_activated(self, _row: Adw.ActionRow, path: str) -> None:
        self._on_open_video(None, path)

    # ── Selection ────────────────────────────────────────────────────

    def _on_check_toggled(self, check: Gtk.CheckButton, path: str) -> None:
        if check.get_active():
            self._selected.add(path)
        else:
            self._selected.discard(path)
        self._update_sel_label()

    def _on_select_all(self, _btn: Gtk.Button) -> None:
        paths = [m.path for m in self._metas]
        all_selected = len(self._selected) == len(paths)
        self._selected = set() if all_selected else set(paths)
        self._update_sel_label()
        self._rebuild_from_cache()

    def _on_delete_selected(self, _btn: Gtk.Button) -> None:
        if not self._selected:
            return
        n = len(self._selected)
        dialog = Adw.AlertDialog(
            heading=_("Delete %d videos?") % n,
            body=_("These videos will be permanently deleted."),
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
            thumb = self._get_thumb_path(p)
            if thumb:
                try:
                    os.remove(thumb)
                except OSError:
                    pass
        self._selected.clear()
        self._update_sel_label()
        self.refresh()

    # ── Thumbnail helpers ────────────────────────────────────────────

    def _get_thumb_path(self, video_path: str) -> str | None:
        thumbs = xdg.thumbs_dir()
        os.makedirs(thumbs, exist_ok=True)
        basename = os.path.splitext(os.path.basename(video_path))[0]
        return os.path.join(thumbs, f"{basename}.jpg")

    def _load_pixbuf(
        self, path: str | None, size: int
    ) -> GdkPixbuf.Pixbuf | None:
        if not path or not os.path.isfile(path):
            return None
        try:
            return GdkPixbuf.Pixbuf.new_from_file_at_scale(path, size, size, True)
        except Exception:
            return None

    def _generate_thumb_file(self, video_path: str, thumb_path: str) -> None:
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", video_path,
                    "-ss", "00:00:01", "-frames:v", "1",
                    "-vf", f"scale={self.THUMB_SIZE}:-1",
                    "-q:v", "5", thumb_path,
                ],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            log.debug("Thumbnail generation failed for %s", video_path, exc_info=True)

    def _get_duration(self, path: str) -> str | None:
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    path,
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            secs = float(result.stdout.strip())
            mins = int(secs // 60)
            secs_rem = int(secs % 60)
            return f"{mins}:{secs_rem:02d}"
        except Exception:
            return None

    # ── Actions ──────────────────────────────────────────────────────

    def _on_open_video(self, _btn: Gtk.Button | None, path: str) -> None:
        uri = GLib.filename_to_uri(path)
        Gtk.show_uri(self.get_root(), uri, Gdk.CURRENT_TIME)

    def _on_open_folder(self, _btn: Gtk.Button) -> None:
        os.makedirs(self._videos_dir, exist_ok=True)
        uri = GLib.filename_to_uri(self._videos_dir)
        Gtk.show_uri(self.get_root(), uri, Gdk.CURRENT_TIME)

    def _on_delete_clicked(self, _btn: Gtk.Button, path: str) -> None:
        dialog = Adw.AlertDialog(
            heading=_("Delete video?"),
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
        thumb = self._get_thumb_path(path)
        if thumb:
            try:
                os.remove(thumb)
            except OSError:
                pass
        self.refresh()
        self.refresh()
