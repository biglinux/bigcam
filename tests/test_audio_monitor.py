"""AudioMonitor.

Two things matter here beyond plain correctness:

1. BigCam must **never disturb the rest of the system's audio**.  It may only
   touch its own sink-inputs, never the default sink, never global volume.
2. A failing source must not turn into an infinite restart / re-detect loop
   that pins a CPU core.
"""

from __future__ import annotations

import threading
import types

import pytest

from core import audio_monitor as am
from core.audio_monitor import AudioMonitor, find_all_audio_sources

PACTL_SOURCES = """\
Source #0
\tState: SUSPENDED
\tName: alsa_output.pci-0000_00_1f.3.analog-stereo.monitor
\tDescription: Monitor of Built-in Audio
\tProperties:
\t\talsa.card = "0"

Source #1
\tState: RUNNING
\tName: alsa_input.usb-046d_HD_Pro_Webcam_C920-02.analog-stereo
\tDescription: HD Pro Webcam C920 Analog Stereo
\tProperties:
\t\talsa.card = "2"
"""


@pytest.fixture
def monitor():
    return AudioMonitor()


class _FakePipeline:
    """Stands in for a Gst.Pipeline in _start_source."""

    def __init__(self) -> None:
        self.state = None

    def get_by_name(self, name):
        return types.SimpleNamespace(set_property=lambda *a: None,
                                     get_property=lambda k: 1.0)

    def get_bus(self):
        return types.SimpleNamespace(
            add_signal_watch=lambda: None, connect=lambda *a: None
        )

    def set_state(self, state):
        self.state = state


@pytest.fixture
def fake_gst(monkeypatch):
    """parse_launch always succeeds; GLib timers are recorded, not run."""
    monkeypatch.setattr(am.Gst, "parse_launch", lambda s: _FakePipeline())
    monkeypatch.setattr(am.GLib, "timeout_add", lambda ms, fn, *a: 1)
    return _FakePipeline


# -- source discovery ------------------------------------------------------


def test_find_sources_skips_monitor_devices(monkeypatch, tmp_path):
    monkeypatch.setattr(am.os, "listdir", lambda p: ["card2"] if "sound" in p else [])
    monkeypatch.setattr(am, "_get_usb_parent", lambda p: "1-2")
    monkeypatch.setattr(am, "_video_label", lambda d: "C920")
    monkeypatch.setattr(
        am.SecureCommandRunner, "run_safe",
        staticmethod(lambda *a, **kw: types.SimpleNamespace(
            returncode=0, stdout=PACTL_SOURCES, stderr="")),
    )
    # video4linux listing has to map the same usb parent
    real_listdir = am.os.listdir

    def _listdir(path):
        if "sound" in path:
            return ["card2"]
        if "video4linux" in path:
            return ["video0"]
        return real_listdir(path)

    monkeypatch.setattr(am.os, "listdir", _listdir)

    sources = find_all_audio_sources()
    names = [s[0] for s in sources]
    assert all(".monitor" not in n for n in names), "monitor source leaked in"
    assert names == ["alsa_input.usb-046d_HD_Pro_Webcam_C920-02.analog-stereo"]


def test_video_label_strips_vid_pid():
    assert am._video_label.__doc__  # sanity
    # _video_label reads sysfs; exercise the regexes directly instead.
    import re
    raw = "HD Pro Webcam C920 (046d:082d): USB Vid"
    raw = re.sub(r"\s*\([\da-fA-F]+:[\da-fA-F]+\).*", "", raw)
    assert raw == "HD Pro Webcam C920"


# -- "do not disturb the system" ------------------------------------------


_FORBIDDEN = (
    "set-sink-volume",
    "set-sink-mute",
    "set-default-sink",
    "set-default-source",
    "set-source-volume",
    "set-source-mute",
    "suspend-sink",
    "unload-module",
)


def test_never_touches_system_wide_audio(monitor, monkeypatch):
    """Every pactl call must be scoped to a *sink-input*, never a sink/source."""
    calls: list[list[str]] = []

    def _run(args, *a, **kw):
        calls.append(list(args))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(am.SecureCommandRunner, "run_safe", staticmethod(_run))

    monitor.add_external_source("ext", "External", pid=0)
    monitor._external["ext"]["index"] = 7
    monitor.set_volume(0.4)
    monitor.set_muted(True)
    monitor.set_muted(False)
    monitor.set_source_volume("ext", 0.9)
    monitor._ensure_sink_inputs_unmuted()

    for call in calls:
        joined = " ".join(call)
        for bad in _FORBIDDEN:
            assert bad not in joined, f"BigCam touched system audio: {joined}"


def test_ensure_unmuted_only_targets_bigcam_sink_inputs(monitor, monkeypatch):
    listing = (
        'Sink Input #10\n\tapplication.name = "Firefox"\n'
        'Sink Input #11\n\tapplication.name = "BigCam"\n'
        'Sink Input #12\n\tapplication.name = "Spotify"\n'
    )
    calls: list[list[str]] = []

    def _run(args, *a, **kw):
        calls.append(list(args))
        if args[:3] == ["pactl", "list", "sink-inputs"]:
            return types.SimpleNamespace(returncode=0, stdout=listing, stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(am.SecureCommandRunner, "run_safe", staticmethod(_run))
    monitor._ensure_sink_inputs_unmuted()

    touched = {c[2] for c in calls if len(c) > 2 and c[1].startswith("set-sink-input")}
    assert touched == {"11"}, f"only BigCam's own sink-input may be changed, got {touched}"


def test_stop_all_also_stops_external_sources(monitor, monkeypatch):
    muted: list[tuple[str, bool]] = []
    monitor.add_external_source(
        "ext", "External",
        volume_cb=lambda v: None,
        mute_cb=lambda m: muted.append(("ext", m)),
    )
    monitor._pipelines["src"] = types.SimpleNamespace(set_state=lambda s: None)
    monitor.stop_all()
    assert monitor._pipelines == {}
    assert ("ext", True) in muted, "external source must be silenced on stop_all"


# -- restart storm protection ---------------------------------------------


def test_start_source_does_not_reset_the_restart_counter(monitor, fake_gst):
    """_start_source is called *by* the restart path — it must not clear the count.

    Otherwise the "give up after 5 attempts" guard can never fire.
    """
    monitor._restart_counts["src"] = 4
    monitor._start_source("src")
    assert monitor._restart_counts.get("src") == 4, (
        "restart counter was reset by _start_source — the give-up limit never fires"
    )


def test_user_toggle_clears_the_restart_counter(monitor, fake_gst):
    """An explicit user action is the only thing allowed to reset the backoff."""
    monitor._restart_counts["src"] = 4
    monitor.toggle_source("src")
    assert monitor._restart_counts.get("src", 0) == 0


def test_eos_restart_gives_up_after_limit(monitor, monkeypatch):
    """A source that keeps hitting EOS must stop being restarted."""
    started: list[str] = []
    scheduled: list[tuple[int, object, tuple]] = []

    monkeypatch.setattr(am.Gst, "parse_launch", lambda s: _FakePipeline())
    real_start = monitor._start_source

    def _start(source):
        started.append(source)
        real_start(source)

    monkeypatch.setattr(monitor, "_start_source", _start)
    monkeypatch.setattr(
        am.GLib, "timeout_add",
        lambda ms, fn, *a: scheduled.append((ms, fn, a)) or 1,
    )

    monitor._pipelines["src"] = _FakePipeline()
    for _ in range(12):
        monitor._on_bus_eos(None, None, "src")
        if scheduled:
            _, fn, args = scheduled.pop()
            fn(*args)

    assert len(started) <= 5, f"restarted {len(started)} times, limit is 5"
    assert "src" not in monitor._pipelines, "source should have been given up on"


def test_error_restart_is_rate_limited(monitor, monkeypatch):
    scheduled: list[int] = []
    monkeypatch.setattr(
        am.GLib, "timeout_add", lambda ms, fn, *a: scheduled.append(ms) or 1
    )
    msg = types.SimpleNamespace(parse_error=lambda: (
        types.SimpleNamespace(message="boom"), "dbg"))

    for _ in range(8):
        monitor._on_bus_error(None, msg, "src")

    assert monitor._restart_counts.get("src", 0) > 0, (
        "error path must share the EOS backoff counter"
    )
    # after the limit it must stop scheduling restarts
    restart_delays = [d for d in scheduled if d < 6000]
    assert len(restart_delays) <= 6, f"unbounded restart storm: {scheduled}"


# -- external source bookkeeping is thread-safe ---------------------------


def test_external_dict_is_not_mutated_during_iteration(monitor):
    stop = threading.Event()
    errors: list[BaseException] = []

    def churn():
        i = 0
        while not stop.is_set():
            monitor.add_external_source(f"s{i % 8}", "x")
            monitor.remove_external_source(f"s{(i + 3) % 8}")
            i += 1

    def reader():
        try:
            while not stop.is_set():
                monitor.set_muted(True)
                _ = monitor.active_source_names
                _ = monitor.sources
                _ = monitor.all_source_names
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=churn), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    threading.Event().wait(0.6)
    stop.set()
    for t in threads:
        t.join(timeout=5)
        assert not t.is_alive()

    assert not errors, f"race on _external: {errors[0]!r}"
