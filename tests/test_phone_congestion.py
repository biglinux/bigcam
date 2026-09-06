"""A slow preview must not starve everything else reading the phone.

The whole frame callback was gated on the preview having been consumed by
GTK.  The recorder and the virtual camera sat behind that gate, so whenever
the preview fell behind, the recording lost frames and every other
application reading the virtual camera stuttered in lockstep with it.

The gate itself is still needed — without it textures queue without bound —
but it belongs around the preview alone.

The sending side of the same Wi-Fi problem is covered by
tests/test_phone_congestion_ladder.py, which runs the client's JS.
"""

from __future__ import annotations

import numpy as np
import pytest

from constants import BackendType
from core.camera_backend import CameraInfo
from core.camera_manager import CameraManager
from core.stream_engine import StreamEngine


# -- the receiver's preview gate -------------------------------------------


@pytest.fixture
def engine(monkeypatch, settings):
    monkeypatch.setattr(CameraManager, "_register_backends", lambda self: None)
    eng = StreamEngine(CameraManager(), settings)
    eng._current_camera = CameraInfo(
        id="phone:1", name="Phone", backend=BackendType.PHONE, device_path="",
    )
    return eng


@pytest.fixture
def frame():
    return np.zeros((48, 64, 3), dtype=np.uint8)


@pytest.fixture
def vcam_pushes(engine, monkeypatch):
    pushed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        engine, "_push_phone_v4l2", lambda bgr, w, h: pushed.append((w, h))
    )
    engine._phone_v4l2_device = "/dev/video20"
    return pushed


def test_virtual_camera_still_gets_frames_while_the_preview_lags(
    engine, frame, vcam_pushes
):
    engine._phone_frame_pending = True          # GTK has not caught up
    engine._on_phone_frame(frame)
    assert vcam_pushes, (
        "the virtual camera was starved because the preview was behind — "
        "every app reading it stutters with the preview"
    )


def test_recorder_still_gets_frames_while_the_preview_lags(
    engine, frame, monkeypatch
):
    written: list[object] = []

    class _Rec:
        is_recording = True

        def write_frame(self, bgr):
            written.append(bgr)

    engine._video_recorder = _Rec()
    engine._phone_frame_pending = True
    engine._on_phone_frame(frame)
    assert written, "dropped a frame from the recording because of the preview"


def test_the_preview_itself_is_still_dropped(engine, frame, monkeypatch):
    """The gate has to keep doing its job: no unbounded texture queue."""
    from core import stream_engine

    posted: list[object] = []
    monkeypatch.setattr(
        stream_engine.GLib, "idle_add", lambda *a, **k: posted.append(a)
    )
    engine._phone_frame_pending = True
    engine._on_phone_frame(frame)
    assert posted == [], "queued a texture while one was already pending"


def test_a_free_preview_is_updated(engine, frame, monkeypatch):
    from core import stream_engine

    posted: list[object] = []
    monkeypatch.setattr(
        stream_engine.GLib, "idle_add", lambda *a, **k: posted.append(a)
    )
    engine._phone_frame_pending = False
    engine._on_phone_frame(frame)
    assert posted, "preview never updated"
    assert engine._phone_frame_pending is True


def test_no_camera_selected_is_a_noop(engine, frame, vcam_pushes):
    engine._current_camera = None
    engine._on_phone_frame(frame)
    assert vcam_pushes == []
