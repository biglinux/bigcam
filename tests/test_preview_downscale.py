"""The preview texture is built at preview size, not sensor size.

Converting a full 1080p frame to BGRA and copying it into a GBytes costs
about 10ms on the asyncio thread — the same thread that has to read the
socket — against 1.3ms once scaled to 1280 wide.  Two 8MB copies dominate,
and PyGObject offers no zero-copy path (handing it the numpy buffer directly
measured 140x *slower*, so that door is closed).

A GTK window never shows more than about a thousand pixels across and
Gtk.Picture rescales whatever it is handed, so the full frame buys nothing
here.  What does need full resolution — photos, recording and the virtual
camera — is served earlier in the callback, before this point.
"""

from __future__ import annotations

import numpy as np
import pytest

from constants import BackendType
from core.camera_backend import CameraInfo
from core.camera_manager import CameraManager
from core.stream_engine import (
    _PREVIEW_MAX_EDGE,
    StreamEngine,
    _downscale_for_preview,
)


def _frame(w: int, h: int):
    return np.zeros((h, w, 3), dtype=np.uint8)


# -- the predicate ---------------------------------------------------------


def test_a_large_frame_is_reduced():
    out = _downscale_for_preview(_frame(1920, 1080), 1920, 1080)
    assert max(out.shape[:2]) == _PREVIEW_MAX_EDGE


def test_aspect_ratio_is_preserved():
    out = _downscale_for_preview(_frame(1920, 1080), 1920, 1080)
    h, w = out.shape[:2]
    assert w / h == pytest.approx(1920 / 1080, abs=0.01)


def test_a_portrait_frame_is_reduced_by_its_long_edge():
    """A phone held upright sends 1080x1920."""
    out = _downscale_for_preview(_frame(1080, 1920), 1080, 1920)
    h, w = out.shape[:2]
    assert h == _PREVIEW_MAX_EDGE
    assert w == 720


@pytest.mark.parametrize("size", [(1280, 720), (640, 480), (320, 240), (1, 1)])
def test_a_frame_at_or_below_the_cap_is_untouched(size):
    w, h = size
    frame = _frame(w, h)
    out = _downscale_for_preview(frame, w, h)
    assert out is frame, "copied a frame that needed no scaling"


def test_a_square_frame_at_the_cap_is_untouched():
    frame = _frame(_PREVIEW_MAX_EDGE, _PREVIEW_MAX_EDGE)
    assert _downscale_for_preview(frame, _PREVIEW_MAX_EDGE, _PREVIEW_MAX_EDGE) is frame


def test_an_extreme_frame_keeps_at_least_one_pixel():
    """A 4000x1 frame must not round the short edge to zero."""
    out = _downscale_for_preview(_frame(4000, 1), 4000, 1)
    h, w = out.shape[:2]
    assert h >= 1 and w == _PREVIEW_MAX_EDGE


# -- what the engine sends to GTK ------------------------------------------


@pytest.fixture
def engine(monkeypatch, settings):
    monkeypatch.setattr(CameraManager, "_register_backends", lambda self: None)
    eng = StreamEngine(CameraManager(), settings)
    eng._current_camera = CameraInfo(
        id="phone:1", name="Phone", backend=BackendType.PHONE, device_path="",
    )
    eng._phone_frame_pending = False
    return eng


@pytest.fixture
def textures(monkeypatch):
    from core import stream_engine

    posted: list[tuple] = []
    monkeypatch.setattr(
        stream_engine.GLib, "idle_add", lambda _fn, *args: posted.append(args)
    )
    return posted


def test_the_texture_is_posted_at_preview_size(engine, textures):
    engine._on_phone_frame(_frame(1920, 1080))
    assert textures, "no texture was posted"
    w, h, stride, _bytes = textures[-1]
    assert (w, h) == (1280, 720)
    assert stride == w * 4, "stride must match the scaled width, not the frame's"


def test_the_byte_count_matches_the_scaled_frame(engine, textures):
    engine._on_phone_frame(_frame(1920, 1080))
    w, h, stride, data = textures[-1]
    assert data.get_size() == stride * h


def test_a_720p_frame_is_posted_unchanged(engine, textures):
    engine._on_phone_frame(_frame(1280, 720))
    w, h, _stride, _data = textures[-1]
    assert (w, h) == (1280, 720)


# -- the full-resolution consumers must not be affected -------------------


def test_the_virtual_camera_still_gets_the_full_frame(engine, monkeypatch):
    pushed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        engine, "_push_phone_v4l2", lambda bgr, w, h: pushed.append(bgr.shape[:2])
    )
    engine._phone_v4l2_device = "/dev/video20"
    engine._on_phone_frame(_frame(1920, 1080))
    assert pushed == [(1080, 1920)], "the virtual camera was given the preview"


def test_the_snapshot_frame_stays_full_resolution(engine):
    engine._on_phone_frame(_frame(1920, 1080))
    assert engine._last_probe_bgr.shape[:2] == (1080, 1920), (
        "photos would be saved at preview resolution"
    )


def test_the_recorder_still_gets_the_full_frame(engine):
    written: list[tuple[int, int]] = []

    class _Rec:
        is_recording = True

        def write_frame(self, bgr):
            written.append(bgr.shape[:2])

    engine._video_recorder = _Rec()
    engine._on_phone_frame(_frame(1920, 1080))
    assert written == [(1080, 1920)]
