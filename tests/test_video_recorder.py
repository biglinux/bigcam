"""VideoRecorder: codec/container negotiation, failure handling, clean finalize."""

from __future__ import annotations

import types

import pytest

from core import video_recorder as vr
from core.video_recorder import VideoRecorder


@pytest.fixture
def recorder():
    return VideoRecorder(camera_manager=None)


@pytest.fixture
def any_encoder(monkeypatch):
    """Pretend every GStreamer element exists."""
    monkeypatch.setattr(vr.Gst.ElementFactory, "find", staticmethod(lambda n: object()))


@pytest.fixture
def sw_only(monkeypatch):
    """Pretend no hardware encoder exists."""
    monkeypatch.setattr(vr.Gst.ElementFactory, "find", staticmethod(lambda n: None))


# -- container / codec negotiation ----------------------------------------


def test_webm_forces_vp9(recorder, sw_only):
    recorder.configure(video_codec="h264", container="webm")
    assert "vp9enc" in recorder._pick_encoder_str()


def test_webm_forces_opus_audio(recorder, sw_only):
    recorder.configure(audio_codec="aac", container="webm")
    assert recorder._pick_audio_encoder_str() == "opusenc"


def test_mp4_rejects_vp9(recorder, sw_only):
    recorder.configure(video_codec="vp9", container="mp4")
    assert "x264enc" in recorder._pick_encoder_str()


def test_muxer_and_extension_match(recorder):
    for container, muxer, ext in (
        ("mkv", "matroskamux", ".mkv"),
        ("mp4", "mp4mux", ".mp4"),
        ("webm", "webmmux", ".webm"),
    ):
        recorder.configure(container=container)
        assert recorder._pick_muxer_str() == muxer
        assert recorder._container_ext() == ext


def test_bitrate_is_clamped(recorder):
    recorder.configure(video_bitrate=999999)
    assert recorder._video_bitrate == 50000
    recorder.configure(video_bitrate=1)
    assert recorder._video_bitrate == 500


def test_hardware_encoder_preferred_when_available(recorder, any_encoder):
    recorder.configure(video_codec="h264")
    assert "nvh264enc" in recorder._pick_encoder_str()


# -- pipeline failure must not turn into a per-frame retry storm ----------


def test_failed_pipeline_aborts_recording(recorder, monkeypatch, bgr_frame):
    """If the pipeline cannot be built, stop — do not rebuild on every frame."""
    attempts = {"n": 0}

    def _boom(_s):
        attempts["n"] += 1
        raise RuntimeError("no encoder")

    monkeypatch.setattr(vr.Gst, "parse_launch", _boom)
    monkeypatch.setattr(vr.xdg, "videos_dir", lambda: "/tmp")

    recorder.start(camera=None, record_audio=False)
    for _ in range(50):
        recorder.write_frame(bgr_frame)

    assert attempts["n"] == 1, (
        f"pipeline rebuilt {attempts['n']} times — one failed frame must abort"
    )
    assert recorder.is_recording is False


def test_state_change_failure_also_aborts(recorder, monkeypatch, bgr_frame):
    built = {"n": 0}

    class _Pipe:
        def get_by_name(self, n):
            return None

        def get_bus(self):
            return types.SimpleNamespace(
                add_signal_watch=lambda: None, connect=lambda *a: None
            )

        def set_state(self, s):
            return vr.Gst.StateChangeReturn.FAILURE

    def _make(_s):
        built["n"] += 1
        return _Pipe()

    monkeypatch.setattr(vr.Gst, "parse_launch", _make)
    monkeypatch.setattr(vr.xdg, "videos_dir", lambda: "/tmp")

    recorder.start(camera=None, record_audio=False)
    for _ in range(30):
        recorder.write_frame(bgr_frame)

    assert built["n"] == 1
    assert recorder.is_recording is False


# -- shutdown --------------------------------------------------------------


def test_wait_finalize_returns_when_nothing_running(recorder):
    recorder.wait_finalize(timeout=0.1)  # must not raise or block


def test_wait_finalize_joins_the_thread(recorder, monkeypatch):
    import threading

    done = threading.Event()

    def _slow():
        done.wait(0.3)

    t = threading.Thread(target=_slow, daemon=True)
    t.start()
    recorder._finalize_thread = t
    done.set()
    recorder.wait_finalize(timeout=5)
    assert not t.is_alive(), "wait_finalize must block until the muxer is done"


def test_stop_without_start_is_a_noop(recorder):
    assert recorder.stop() is None


def test_output_path_uses_configured_extension(recorder, monkeypatch, tmp_path):
    monkeypatch.setattr(vr.xdg, "videos_dir", lambda: str(tmp_path))
    recorder.configure(container="mp4")
    path = recorder.start(camera=None, record_audio=False)
    assert path.endswith(".mp4")
    recorder._recording = False
