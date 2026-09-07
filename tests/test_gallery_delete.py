"""Deleting from a gallery must be recoverable, and thumbnails must not collide.

Two independent defects in the galleries.

A photo or a recording is the one thing in BigCam a user cannot recreate, and
both galleries unlinked it outright — one mis-click on a bulk selection and
it was gone.  Deletion goes through the freedesktop trash now, so the file
can be restored from the file manager, with an unlink only as a fallback for
filesystems that have no trash.

Separately, video thumbnails were named after the file's stem alone:

    /videos/holiday.mp4  ->  holiday.jpg
    /videos/holiday.mkv  ->  holiday.jpg
    /other/holiday.mp4   ->  holiday.jpg

Whichever was scanned first generated the thumbnail and the others showed its
frame.  Deleting any one of them removed the shared thumbnail from under the
rest.
"""

from __future__ import annotations

import pytest

from ui.photo_gallery import _delete_to_trash as photo_trash
from ui.video_gallery import VideoGallery, _delete_to_trash as video_trash
from ui.video_gallery import _safe_stem


# -- trashing --------------------------------------------------------------


@pytest.fixture(params=[video_trash, photo_trash], ids=["video", "photo"])
def trash(request):
    return request.param


def test_the_file_goes_to_the_trash_not_the_void(trash, tmp_path, monkeypatch):
    from ui import video_gallery, photo_gallery

    trashed: list[str] = []

    class _GFile:
        def __init__(self, path):
            self._path = path

        def trash(self, _cancellable):
            trashed.append(self._path)
            return True

    for mod in (video_gallery, photo_gallery):
        monkeypatch.setattr(mod.Gio.File, "new_for_path", _GFile)

    target = tmp_path / "holiday.mp4"
    target.write_bytes(b"data")
    assert trash(str(target)) is True
    assert trashed == [str(target)]
    assert target.exists(), "unlinked the file as well as trashing it"


def test_an_unlink_is_the_fallback_when_trash_is_unavailable(
    trash, tmp_path, monkeypatch
):
    """A removable drive may have no .Trash-$uid; the dialog was confirmed."""
    from ui import video_gallery, photo_gallery

    class _GFile:
        def __init__(self, path):
            pass

        def trash(self, _cancellable):
            raise video_gallery.GLib.Error("not supported")

    for mod in (video_gallery, photo_gallery):
        monkeypatch.setattr(mod.Gio.File, "new_for_path", _GFile)

    target = tmp_path / "clip.mp4"
    target.write_bytes(b"data")
    assert trash(str(target)) is True
    assert not target.exists()


def test_a_missing_file_reports_failure_without_raising(
    trash, tmp_path, monkeypatch
):
    from ui import video_gallery, photo_gallery

    class _GFile:
        def __init__(self, path):
            pass

        def trash(self, _cancellable):
            raise video_gallery.GLib.Error("no trash")

    for mod in (video_gallery, photo_gallery):
        monkeypatch.setattr(mod.Gio.File, "new_for_path", _GFile)

    assert trash(str(tmp_path / "gone.mp4")) is False


# -- thumbnail identity ----------------------------------------------------


@pytest.fixture
def thumb_path(monkeypatch, tmp_path):
    from utils import xdg

    monkeypatch.setattr(xdg, "thumbs_dir", lambda: str(tmp_path / "thumbs"))
    gallery = VideoGallery.__new__(VideoGallery)
    return lambda path: VideoGallery._get_thumb_path(gallery, path)


def test_same_stem_different_extension_gets_its_own_thumbnail(thumb_path):
    assert thumb_path("/videos/holiday.mp4") != thumb_path("/videos/holiday.mkv")


def test_same_name_different_directory_gets_its_own_thumbnail(thumb_path):
    assert thumb_path("/videos/holiday.mp4") != thumb_path("/other/holiday.mp4")


def test_the_same_file_always_maps_to_the_same_thumbnail(thumb_path):
    assert thumb_path("/videos/holiday.mp4") == thumb_path("/videos/holiday.mp4")


def test_a_relative_path_matches_its_absolute_form(thumb_path, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert thumb_path("clip.mp4") == thumb_path(str(tmp_path / "clip.mp4"))


def test_the_thumbnail_keeps_a_readable_prefix(thumb_path):
    import os

    assert os.path.basename(thumb_path("/videos/holiday.mp4")).startswith("holiday-")


def test_the_thumbnail_directory_is_created(thumb_path, tmp_path):
    thumb_path("/videos/holiday.mp4")
    assert (tmp_path / "thumbs").is_dir()


# -- the readable prefix is only cosmetic ----------------------------------


@pytest.mark.parametrize(
    "basename,expected",
    [
        ("holiday.mp4", "holiday"),
        ("my video (1).mkv", "my_video_1_"),
        ("../../etc/passwd", "passwd"),
        ("a/b.mp4", "b"),
        (".mp4", "mp4"),        # a leading dot is a hidden file, not an ext
        ("", "video"),
    ],
)
def test_stem_is_filename_safe(basename, expected):
    import os

    assert _safe_stem(os.path.basename(basename)) == expected


def test_a_very_long_name_is_capped():
    assert len(_safe_stem("x" * 500)) == 48
