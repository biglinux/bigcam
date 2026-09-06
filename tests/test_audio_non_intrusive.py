"""BigCam must be a well-behaved guest on the system's audio graph.

Two concrete hazards this pins down:

1. Forcing a fixed buffer-time/latency-time on ``pulsesrc`` asks PipeWire for a
   specific quantum.  A filter chain running with ``node.lock-quantum`` (the
   BigLinux AI microphone does) can be knocked over by that renegotiation.
2. Writing volume levels onto a stream makes ``module-stream-restore``
   memorise them, so the change outlives the process.
"""

from __future__ import annotations

import inspect
import types

import pytest

from core import audio_monitor as am
from core.audio_monitor import AudioMonitor


@pytest.fixture
def monitor():
    return AudioMonitor()


@pytest.fixture
def captured_pipeline(monkeypatch):
    """Capture the pipeline string _start_source builds, without running it."""
    seen: list[str] = []

    class _Pipe:
        def get_by_name(self, name):
            return types.SimpleNamespace(
                set_property=lambda *a: None, get_property=lambda k: 1.0
            )

        def get_bus(self):
            return types.SimpleNamespace(
                add_signal_watch=lambda: None, connect=lambda *a: None
            )

        def set_state(self, state):
            pass

    def _parse(s):
        seen.append(s)
        return _Pipe()

    monkeypatch.setattr(am.Gst, "parse_launch", _parse)
    monkeypatch.setattr(am.GLib, "timeout_add", lambda ms, fn, *a: 1)
    return seen


# -- do not dictate the graph's quantum -----------------------------------


def test_pulsesrc_does_not_force_a_quantum(monitor, captured_pipeline):
    """Let PipeWire negotiate; a hard-coded buffer-time forces a quantum change."""
    monitor._start_source("some.mic")
    assert captured_pipeline, "no pipeline was built"
    pipeline = captured_pipeline[0]
    for prop in ("buffer-time=", "latency-time="):
        assert prop not in pipeline, (
            f"pulsesrc still pins {prop} — this renegotiates the graph quantum "
            f"and can stall a filter chain using node.lock-quantum"
        )


def test_pipeline_still_has_the_essential_elements(monitor, captured_pipeline):
    monitor._start_source("some.mic")
    pipeline = captured_pipeline[0]
    for element in ("pulsesrc", "audioconvert", "audioresample", "volume", "queue"):
        assert element in pipeline, f"{element} missing from the monitor pipeline"


def test_volume_element_name_is_collision_free(monitor, captured_pipeline):
    """Two sources must never end up sharing a GStreamer element name."""
    monitor._start_source("device.one")
    monitor._start_source("device.two")
    names = set()
    for pipeline in captured_pipeline:
        for token in pipeline.split():
            if token.startswith("name=vol"):
                names.add(token)
    assert len(names) == len(captured_pipeline), (
        f"volume element names collided: {names}"
    )


# -- never write volume onto streams the system remembers -----------------


def test_does_not_force_volume_on_its_own_sink_inputs(monitor, monkeypatch):
    """set-sink-input-volume is persisted by module-stream-restore."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        am.SecureCommandRunner, "run_safe",
        staticmethod(lambda args, *a, **kw: calls.append(list(args)) or
                     types.SimpleNamespace(
                         returncode=0,
                         stdout='Sink Input #7\n\tapplication.name = "BigCam"\n',
                         stderr="")),
    )
    monitor._ensure_sink_inputs_unmuted()

    volume_writes = [c for c in calls if "set-sink-input-volume" in c]
    assert not volume_writes, (
        f"BigCam forces a volume level that outlives the process: {volume_writes}"
    )


def test_still_unmutes_its_own_sink_inputs(monitor, monkeypatch):
    """Unmuting is the point of the call and must be kept."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        am.SecureCommandRunner, "run_safe",
        staticmethod(lambda args, *a, **kw: calls.append(list(args)) or
                     types.SimpleNamespace(
                         returncode=0,
                         stdout='Sink Input #7\n\tapplication.name = "BigCam"\n',
                         stderr="")),
    )
    monitor._ensure_sink_inputs_unmuted()
    unmutes = [c for c in calls if "set-sink-input-mute" in c]
    assert unmutes, "BigCam should still unmute its own stream"
    assert unmutes[0][2] == "7"
    assert unmutes[0][3] == "0"


def test_no_global_audio_commands_anywhere_in_the_module():
    """Static guard: the module must never name a system-wide pactl verb."""
    src = inspect.getsource(am)
    forbidden = (
        "set-sink-volume", "set-sink-mute",
        "set-source-volume", "set-source-mute",
        "set-default-sink", "set-default-source",
        "suspend-sink", "suspend-source",
        "unload-module", "load-module",
    )
    for verb in forbidden:
        assert verb not in src, f"audio_monitor references a system-wide verb: {verb}"
