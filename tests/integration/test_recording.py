"""Real encoders and private PulseAudio streams; no physical input devices."""

import errno
import json
import os
import struct
import subprocess
import sys
import time
from itertools import pairwise
from pathlib import Path
from unittest.mock import patch

import gi
import numpy as np
import pytest

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "usr/share/biglinux/bigcam")
)
from core.audio_monitor import AudioMonitor
from core.phone_camera import PhoneCameraServer
from core.video_recorder import VideoRecorder
from utils import xdg

Gst.init(None)


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    monkeypatch.setattr(xdg, "videos_dir", lambda: str(tmp_path))
    instance = VideoRecorder()
    yield instance
    instance.stop()
    assert instance.wait_finalize(20)


def feed(recorder, seconds=2, audio=None):
    image = np.full((48, 66, 3), 120, dtype=np.uint8)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        recorder.write_frame(image)
        if audio:
            audio()
        time.sleep(0.02)


def finish(recorder):
    recorder.stop()
    assert recorder.wait_finalize(20)
    assert recorder.state == "idle"
    return recorder.output_path


def samples(path):
    raw = subprocess.check_output(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "f32le",
            "-",
        ],
        timeout=15,
    )
    data = np.frombuffer(raw, dtype="<f4")
    assert len(data) > 16000
    return data[-24000:-8000]


def amplitude(data, hz):
    return abs(np.fft.rfft(data)[hz])


def test_browser_pcm_reaches_recording_without_playback(recorder):
    phone = PhoneCameraServer()
    phone._owner = "test"
    # Keep the playback worker inactive. Recording must not depend on speakers.
    phone._audio_thread = type(
        "InactivePlayback", (), {"is_alive": lambda self: True}
    )()
    phone.set_audio_callback(lambda pcm: recorder.write_audio("phone_browser", pcm))
    recorder.start(
        None,
        audio_sources=["phone_browser"],
        active_audio_sources=["phone_browser"],
        external_audio={"phone_browser": None},
    )
    count = 0

    def packet():
        nonlocal count
        t = (np.arange(320) + 320 * count) / 16000
        pcm = (np.sin(2 * np.pi * 660 * t) * 12000).astype("<i2").tobytes()
        phone.receive(b"\x01" + struct.pack(">I", count) + pcm, "test")
        count += 1

    feed(recorder, 3, packet)
    data = samples(finish(recorder))
    assert amplitude(data, 660) > amplitude(data, 880) * 30


def test_silent_browser_does_not_block_video_eos(recorder):
    recorder.start(
        None,
        audio_sources=["phone_browser"],
        active_audio_sources=["phone_browser"],
        external_audio={"phone_browser": None},
    )
    feed(recorder)
    finish(recorder)


def test_external_capture_excludes_other_playback(recorder):
    players = []
    try:
        for frequency in (440, 880):
            players.append(
                subprocess.Popen(
                    [
                        "gst-launch-1.0",
                        "-q",
                        "audiotestsrc",
                        "is-live=true",
                        f"freq={frequency}",
                        "!",
                        "audioconvert",
                        "!",
                        "pulsesink",
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        deadline = time.monotonic() + 5
        while AudioMonitor._find_sink_input_by_pid(players[0].pid) is None:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        recorder.start(
            None,
            audio_sources=["android"],
            active_audio_sources=["android"],
            external_audio={"android": players[0].pid},
        )
        feed(recorder, 3)
        data = samples(finish(recorder))
        assert amplitude(data, 440) > amplitude(data, 880) * 30
        index = AudioMonitor._find_sink_input_by_pid(players[0].pid)
        subprocess.run(
            ["pactl", "--", "set-sink-input-volume", str(index), "-12dB"],
            check=True,
            timeout=5,
        )
        recorder.start(
            None,
            audio_sources=["android"],
            active_audio_sources=["android"],
            source_volumes={"android": 0.25},
            external_audio={"android": players[0].pid},
        )
        feed(recorder, 3)
        quieter = samples(finish(recorder))
        ratio = np.sqrt(np.mean(quieter**2) / np.mean(data**2))
        assert 0.18 < ratio < 0.32, ratio
        assert all(
            process.poll() is not None for process, _thread in recorder._audio_processes
        )
    finally:
        for process in players:
            process.terminate()
            process.wait(timeout=5)


def test_storage_error_is_not_reported_as_saved(recorder):
    outcomes = []
    recorder.connect(
        "finalized", lambda _r, _path, ok, error: outcomes.append((ok, error))
    )
    recorder.start(None, record_audio=False)
    feed(recorder)
    with patch.object(
        os, "fsync", side_effect=OSError(errno.EIO, "simulated disk error")
    ):
        recorder.stop()
        assert recorder.wait_finalize(20)
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)
    assert recorder.state == "error"
    assert outcomes and outcomes[-1][0] is False
    assert "simulated disk error" in outcomes[-1][1]
    assert Path(recorder.output_path).stat().st_size > 0


def test_initial_encoder_delay_does_not_duplicate_video_timestamps(recorder):
    recorder.start(None, record_audio=False)
    feed(recorder)
    path = finish(recorder)
    frames = json.loads(
        subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v",
                "-show_frames",
                "-show_entries",
                "frame=pts_time",
                "-of",
                "json",
                path,
            ],
            timeout=10,
        )
    )["frames"]
    timestamps = [float(frame["pts_time"]) for frame in frames]
    assert len(timestamps) > 10
    assert all(b > a for a, b in pairwise(timestamps))
