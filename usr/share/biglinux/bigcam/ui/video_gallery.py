"""Video gallery – thumbnail grid of recorded videos."""

from __future__ import annotations

import os
import subprocess
import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, Gdk, GdkPixbuf, GLib

from utils import xdg
from utils.i18n import _

log = logging.getLogger(__name__)


class VideoGallery(Gtk.ScrolledWindow):
    """Grid of recorded video thumbnails."""

    THUMB_SIZE = 160

    def __init__(self) -> None:
        super().__init__(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        self._videos_dir = xdg.videos_dir()

        vbox = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            margin_top=12,
            margin_bottom=12,
            margin_start=12,
            margin_end=12,
        )

        # Header toolbar
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title = Gtk.Label(label=_("Recorded Videos"), hexpand=True, xalign=0)
        title.add_css_class("title-4")

        open_btn = Gtk.Button.new_from_icon_name("folder-open-symbolic")
        open_btn.add_css_class("flat")
        open_btn.set_tooltip_text(_("Open videos folder"))
        open_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Open videos folder")]
        )
        open_btn.connect("clicked", self._on_open_folder)

        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.add_css_class("flat")
        refresh_btn.set_tooltip_text(_("Refresh"))
        refresh_btn.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Refresh video gallery")]
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
            icon_name="video-display-symbolic",
            title=_("No videos yet"),
            description=_("Recorded videos will appear here."),
        )
        self._empty.set_visible(False)
        vbox.append(self._empty)

        self.set_child(vbox)
        self.connect("map", self._on_mapped)

    def _on_mapped(self, _widget: Gtk.Widget) -> None:
        self.refresh()

    def refresh(self) -> None:
        """Scan videos directory and rebuild the grid."""
        child = self._flowbox.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self._flowbox.remove(child)
            child = next_child

        videos = self._list_videos()
        self._empty.set_visible(len(videos) == 0)

        for path in videos[:100]:
            thumb = self._make_thumbnail(path)
            if thumb:
                self._flowbox.append(thumb)

    def _list_videos(self) -> list[str]:
        if not os.path.isdir(self._videos_dir):
            return []
        files: list[str] = []
        exts = (".mkv", ".mp4", ".webm", ".avi", ".mov")
        for entry in sorted(
            os.scandir(self._videos_dir),
            key=lambda e: e.stat().st_mtime,
            reverse=True,
        ):
            if entry.is_file() and entry.name.lower().endswith(exts):
                files.append(entry.path)
        return files

    def _make_thumbnail(self, path: str) -> Gtk.Widget | None:
        thumb_path = self._get_thumb_path(path)
        if thumb_path and os.path.isfile(thumb_path):
            pixbuf = self._load_pixbuf(thumb_path)
        else:
            pixbuf = self._generate_thumb(path, thumb_path)
        if pixbuf is None:
            return self._make_placeholder(path)

        texture = Gdk.Texture.new_for_pixbuf(pixbuf)
        picture = Gtk.Picture.new_for_paintable(texture)
        picture.set_content_fit(Gtk.ContentFit.COVER)
        picture.set_size_request(self.THUMB_SIZE, self.THUMB_SIZE)
        picture.add_css_class("card")

        # Inner overlay for play icon and duration label on the picture
        inner_overlay = Gtk.Overlay()
        inner_overlay.set_child(picture)

        play_icon = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        play_icon.set_pixel_size(32)
        play_icon.set_opacity(0.8)
        play_icon.set_halign(Gtk.Align.CENTER)
        play_icon.set_valign(Gtk.Align.CENTER)
        inner_overlay.add_overlay(play_icon)

        duration = self._get_duration(path)
        if duration:
            dur_label = Gtk.Label(label=duration)
            dur_label.add_css_class("caption")
            dur_label.set_halign(Gtk.Align.END)
            dur_label.set_valign(Gtk.Align.END)
            dur_label.set_margin_end(4)
            dur_label.set_margin_bottom(4)
            dur_box = Gtk.Box()
            dur_box.append(dur_label)
            dur_box.add_css_class("osd")
            dur_box.set_halign(Gtk.Align.END)
            dur_box.set_valign(Gtk.Align.END)
            dur_box.set_margin_end(4)
            dur_box.set_margin_bottom(4)
            inner_overlay.add_overlay(dur_box)

        # Open button (main clickable area)
        open_btn = Gtk.Button()
        open_btn.set_child(inner_overlay)
        open_btn.add_css_class("flat")
        open_btn.set_tooltip_text(os.path.basename(path))
        open_btn.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Play %s") % os.path.basename(path)],
        )
        open_btn.connect("clicked", self._on_open_video, path)

        # Outer overlay: open button as base, delete button on top
        outer_overlay = Gtk.Overlay()
        outer_overlay.set_child(open_btn)

        del_btn = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        del_btn.add_css_class("osd")
        del_btn.add_css_class("circular")
        del_btn.add_css_class("delete-thumb-btn")
        del_btn.set_halign(Gtk.Align.END)
        del_btn.set_valign(Gtk.Align.START)
        del_btn.set_margin_end(4)
        del_btn.set_margin_top(4)
        del_btn.set_tooltip_text(_("Delete"))
        del_btn.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Delete %s") % os.path.basename(path)],
        )
        del_btn.connect("clicked", self._on_delete_clicked, path)
        outer_overlay.add_overlay(del_btn)

        return outer_overlay

    def _make_placeholder(self, path: str) -> Gtk.Widget:
        icon = Gtk.Image.new_from_icon_name("video-x-generic-symbolic")
        icon.set_pixel_size(48)
        icon.set_size_request(self.THUMB_SIZE, self.THUMB_SIZE)
        icon.add_css_class("card")

        btn = Gtk.Button()
        btn.set_child(icon)
        btn.add_css_class("flat")
        btn.set_tooltip_text(os.path.basename(path))
        btn.update_property(
            [Gtk.AccessibleProperty.LABEL],
            [_("Play %s") % os.path.basename(path)],
        )
        btn.connect("clicked", self._on_open_video, path)
        return btn

    def _get_thumb_path(self, video_path: str) -> str | None:
        thumbs = xdg.thumbs_dir()
        os.makedirs(thumbs, exist_ok=True)
        basename = os.path.splitext(os.path.basename(video_path))[0]
        return os.path.join(thumbs, f"{basename}.jpg")

    def _load_pixbuf(self, path: str) -> GdkPixbuf.Pixbuf | None:
        try:
            return GdkPixbuf.Pixbuf.new_from_file_at_scale(
                path, self.THUMB_SIZE, self.THUMB_SIZE, True
            )
        except Exception:
            return None

    def _generate_thumb(
        self, video_path: str, thumb_path: str | None
    ) -> GdkPixbuf.Pixbuf | None:
        if not thumb_path:
            return None
        try:
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    video_path,
                    "-ss",
                    "00:00:01",
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale={self.THUMB_SIZE}:-1",
                    "-q:v",
                    "5",
                    thumb_path,
                ],
                capture_output=True,
                timeout=10,
            )
            if os.path.isfile(thumb_path):
                return self._load_pixbuf(thumb_path)
        except Exception:
            log.debug("Thumbnail generation failed", exc_info=True)
        return None

    def _get_duration(self, path: str) -> str | None:
        try:
            result = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
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

    def _on_open_video(self, _btn: Gtk.Button, path: str) -> None:
        """Open video in default system player."""
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
        # Also remove cached thumbnail
        thumb_path = self._get_thumb_path(path)
        if thumb_path:
            try:
                os.remove(thumb_path)
            except OSError:
                pass
        self.refresh()
