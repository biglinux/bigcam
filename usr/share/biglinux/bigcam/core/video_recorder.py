"""Bounded video recording with a single pipeline owner and confirmed EOS.

The GTK thread never builds, blocks on or finalizes a recording pipeline.
Preview may drop frames; recording reports its own dropped-frame count and uses
capture timestamps, rather than speeding up the movie when the consumer is slow.
"""
from __future__ import annotations

import json
import logging
import math
import os
import queue
import subprocess
import threading
import time
from typing import Any

import cv2
import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, GObject, Gst
from utils import xdg
from utils.gst_buffers import bgr_buffer
from utils.media_paths import reserve_media_path, reserve_named_path
from utils.urls import gst_quote
from utils.video_formats import frame_rate

from core.recording_config import RecordingConfig

log = logging.getLogger(__name__)


class VideoRecorder(GObject.Object):
    """State: idle -> starting -> recording -> finalizing -> idle/error.

    ``finalized`` is emitted only after EOS, NULL and a nonempty file have been
    observed (or with success=False and an actionable error). The caller must
    retain the application until this signal arrives. No remux discards the
    original recording, and a second start cannot overwrite a pending session.
    """
    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "finalized": (GObject.SignalFlags.RUN_LAST, None, (str, bool, str)),
    }

    def __init__(self, camera_manager=None):
        super().__init__()
        self._manager = camera_manager
        self._lock = threading.RLock()
        self._config = RecordingConfig()
        self._session_config = self._config
        self._state = "idle"
        self._output_path = ""
        self._frames = queue.Queue(maxsize=4)
        self._stop_event = threading.Event()
        self._worker = None
        self._done = threading.Event()
        self._done.set()
        self._muted = False
        self._volumes = {}
        self._active = set()
        self._audio_devices = []
        self._external_audio = {}
        self._pcm_sources = {}
        self._audio_processes = []
        self.dropped_frames = 0
        self.frames_written = 0

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def is_recording(self):
        return self.state in {"starting", "recording"}

    @property
    def is_finalizing(self):
        return self.state == "finalizing"

    @property
    def output_path(self):
        return self._output_path

    def _set_state(self, state):
        with self._lock:
            self._state = state
        GLib.idle_add(self._emit_state, state)

    def _emit_state(self, state):
        self.emit("state-changed", state)
        return GLib.SOURCE_REMOVE

    def configure(self, video_codec="h264", audio_codec="opus", container="mkv", video_bitrate=8000):
        config = RecordingConfig(video_codec, audio_codec, container, video_bitrate)
        with self._lock:
            self._config = config  # Applies only to the next recording.

    def start(self, camera, pipeline=None, filename=None, mirror=False,
              record_audio=True, audio_sources=None, active_audio_sources=None,
              source_volumes=None, muted=False, fps=30.0, external_audio: dict[str, int | None] | None = None):
        del camera, pipeline, mirror  # The preview mirror never changes saved pixels.
        with self._lock:
            if not self._done.is_set():
                return None
            self._session_config = config = self._config
            self._fps = frame_rate(fps if fps and fps > 0 else 30)
            self._output_path = (reserve_named_path(xdg.videos_dir(), filename)
                                 if filename else reserve_media_path(xdg.videos_dir(), config.extension))
            self._audio_devices = list(dict.fromkeys(audio_sources or [])) if record_audio else []
            self._external_audio = dict(external_audio or {})
            self._pcm_sources = {}
            self._audio_processes = []
            self._active = set(active_audio_sources or [])
            self._volumes = dict(source_volumes or {})
            self._muted = bool(muted)
            self._frames = queue.Queue(maxsize=4)
            self._stop_event = threading.Event()
            self._done.clear()
            self.frames_written = self.dropped_frames = 0
            self._set_state("starting")
            self._worker = threading.Thread(target=self._record, name="bigcam-recording", daemon=False)
            self._worker.start()
            return self._output_path

    def write_frame(self, bgr: Any):
        if not self.is_recording or self._stop_event.is_set():
            return
        if bgr is None or bgr.ndim != 3 or bgr.shape[2] != 3:
            return
        item = (time.monotonic_ns(), bgr.copy())
        try:
            self._frames.put_nowait(item)
        except queue.Full:
            try:
                self._frames.get_nowait()
                self.dropped_frames += 1
            except queue.Empty:
                pass
            try:
                self._frames.put_nowait(item)
            except queue.Full:
                self.dropped_frames += 1

    def write_audio(self, name: str, pcm: bytes) -> None:
        with self._lock:
            source = self._pcm_sources.get(name)
            if source is not None and not self._stop_event.is_set():
                source.emit("push-buffer", Gst.Buffer.new_wrapped(pcm))

    def _capture_playback(self, name, pid):
        from core.audio_monitor import AudioMonitor
        index = AudioMonitor._find_sink_input_by_pid(pid)
        if index is None:
            raise RuntimeError(f"No playback audio is available for {name}")
        inputs = json.loads(subprocess.check_output(
            ["pactl", "-f", "json", "list", "sink-inputs"], text=True, timeout=5))
        sink = next(item["sink"] for item in inputs if item["index"] == index)
        sinks = json.loads(subprocess.check_output(
            ["pactl", "-f", "json", "list", "sinks"], text=True, timeout=5))
        monitor = next(item["monitor_source"] for item in sinks if item["index"] == sink)
        # PulseAudio filters this monitor to one sink-input. Never record the
        # whole desktop monitor or substitute the user's default microphone.
        process = subprocess.Popen(
            ["parec", f"--monitor-stream={index}", f"--device={monitor}", "--raw",
             "--format=s16le", "--rate=48000", "--channels=2", "--latency-msec=40"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        def read():
            while pcm := process.stdout.read(3840):
                self.write_audio(name, pcm)
        thread = threading.Thread(target=read, name="bigcam-record-audio", daemon=True)
        self._audio_processes.append((process, thread))
        thread.start()

    def stop(self):
        """Request asynchronous EOS; the return value is NOT a saved-file result."""
        if not self._done.is_set():
            self._set_state("finalizing")
            self._stop_event.set()
        return None

    def wait_finalize(self, timeout=20.0):
        """Emergency shutdown/test hook. Do not call on the interactive GTK loop."""
        return self._done.wait(timeout)

    def set_source_active(self, source_name, active):
        with self._lock:
            if active:
                self._active.add(source_name)
            else:
                self._active.discard(source_name)

    def set_source_volume(self, source_name, volume):
        value = float(volume)
        if not math.isfinite(value):
            return
        with self._lock:
            self._volumes[source_name] = max(0.0, min(1.0, value))

    def set_muted(self, muted):
        with self._lock:
            self._muted = bool(muted)

    def _volume(self, device):
        with self._lock:
            if self._muted or device not in self._active:
                return 0.0
            if self._external_audio.get(device):
                return 1.0  # Playback gain has already been applied by PulseAudio.
            value = float(self._volumes.get(device, 1.0))
            return max(0.0, min(1.0, value)) if math.isfinite(value) else 0.0

    def _select_encoder(self, w, h):
        config = self._session_config
        for name, encoder in config.encoders():
            if self._stop_event.is_set():
                raise RuntimeError("Recording cancelled before the encoder was ready")
            if not Gst.ElementFactory.find(name):
                continue
            probe = None
            try:
                probe = Gst.parse_launch(
                    f"videotestsrc num-buffers=2 ! video/x-raw,width={w},height={h},framerate=30/1 ! "
                    f"videoconvert ! {encoder} ! fakesink sync=false")
                if probe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                    continue
                message = probe.get_bus().timed_pop_filtered(
                    3 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
                if message and message.type == Gst.MessageType.EOS:
                    log.info("Recording encoder verified: %s", name)
                    return encoder
                log.warning("Encoder %s did not complete a test encode; trying fallback", name)
            except GLib.Error:
                log.warning("Encoder %s could not be configured", name)
            finally:
                if probe is not None:
                    probe.set_state(Gst.State.NULL)
        raise RuntimeError("No usable video encoder is installed")

    def _audio_encoder(self):
        candidates = {"aac": ["avenc_aac", "fdkaacenc", "voaacenc"],
                      "mp3": ["lamemp3enc"], "vorbis": ["vorbisenc"], "opus": ["opusenc"]}
        for name in candidates[self._session_config.audio_codec]:
            if Gst.ElementFactory.find(name):
                return name
        raise RuntimeError("The selected audio encoder is not installed")

    def _build(self, w, h):
        encoder = self._select_encoder(w, h)
        rate = self._fps
        desc = (f"appsrc name=vsrc format=time is-live=true block=false max-buffers=4 "
                f"leaky-type=downstream caps=video/x-raw,format=BGR,width={w},height={h},framerate={rate} ! "
                f"queue max-size-buffers=4 max-size-bytes=0 max-size-time=0 ! videoconvert ! {encoder} ! "
                f"{self._session_config.muxer} name=mux ! filesink location={gst_quote(self._output_path)} ")
        if self._audio_devices:
            desc += (f"audiomixer name=amix ignore-inactive-pads=true latency=100000000 ! audioconvert ! "
                     f"audioresample ! {self._audio_encoder()} ! queue ! mux. "
                     "audiotestsrc name=asilence is-live=true wave=silence ! "
                     "audio/x-raw,rate=48000,channels=2 ! amix. ")
            for i, device in enumerate(self._audio_devices):
                if device in self._external_audio:
                    rate, channels = (48000, 2) if self._external_audio[device] else (16000, 1)
                    desc += (f"appsrc name=asrc_{i} format=time is-live=true do-timestamp=true "
                             f"block=false max-buffers=8 leaky-type=downstream "
                             f"caps=audio/x-raw,format=S16LE,rate={rate},channels={channels},layout=interleaved ! ")
                else:
                    desc += (f"pulsesrc device={gst_quote(device)} name=asrc_{i} "
                             f"do-timestamp=true provide-clock=false buffer-time=200000 latency-time=50000 ! ")
                desc += (
                         f"queue max-size-time=500000000 max-size-buffers=0 max-size-bytes=0 leaky=downstream ! "
                         f"audioconvert ! audioresample ! audio/x-raw,rate=48000,channels=2 ! volume name=avol_{i} volume={self._volume(device)} ! amix. ")
        pipeline = Gst.parse_launch(desc)
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(Gst.State.NULL)
            raise RuntimeError("Failed to start the recording pipeline")
        return pipeline

    @staticmethod
    def _check_bus(bus):
        message = bus.pop_filtered(Gst.MessageType.ERROR)
        if message:
            err, _debug = message.parse_error()
            raise RuntimeError(err.message)

    def _record(self):
        pipeline = None
        error = ""
        success = False
        path = self._output_path
        try:
            deadline = time.monotonic() + 15
            first = None
            while first is None:
                if self._stop_event.is_set() or time.monotonic() >= deadline:
                    raise RuntimeError("No video frame received before recording stopped")
                try:
                    first = self._frames.get(timeout=0.1)
                except queue.Empty:
                    pass
            _, image = first
            h, w = image.shape[:2]
            w, h = max(2, w // 2 * 2), max(2, h // 2 * 2)
            pipeline = self._build(w, h)
            with self._lock:
                self._pcm_sources = {device: pipeline.get_by_name(f"asrc_{i}")
                                     for i, device in enumerate(self._audio_devices)
                                     if device in self._external_audio}
            for device, pid in self._external_audio.items():
                if pid and device in self._audio_devices:
                    self._capture_playback(device, pid)
            bus = pipeline.get_bus()
            vsrc = pipeline.get_by_name("vsrc")
            volumes = [(device, pipeline.get_by_name(f"avol_{i}"))
                       for i, device in enumerate(self._audio_devices)]
            origin = time.monotonic_ns()
            last_pts = -1
            self._set_state("recording")
            # Encoding probes can take seconds. Do not squeeze queued startup
            # frames into identical mux timestamps at the start of the movie.
            pending = (origin, first[1])
            while not self._frames.empty():
                try:
                    self._frames.get_nowait()
                    self.dropped_frames += 1
                except queue.Empty:
                    break
            last_frame_at = time.monotonic()
            while not self._stop_event.is_set() or not self._frames.empty() or pending is not None:
                self._check_bus(bus)
                if any(process.poll() is not None for process, _thread in self._audio_processes):
                    raise RuntimeError("External audio capture stopped unexpectedly")
                for device, volume in volumes:
                    volume.set_property("volume", self._volume(device))
                if pending is None:
                    try:
                        pending = self._frames.get(timeout=0.1)
                    except queue.Empty:
                        if time.monotonic() - last_frame_at > 10:
                            raise RuntimeError("The camera stopped supplying frames")
                        continue
                captured, frame = pending
                pending = None
                last_frame_at = time.monotonic()
                if frame.shape[:2] != (h, w):
                    frame = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
                buffer = bgr_buffer(frame)
                buffer.pts = max(last_pts + 1, captured - origin, 0)
                buffer.dts = Gst.CLOCK_TIME_NONE
                last_pts = buffer.pts
                if vsrc.emit("push-buffer", buffer) != Gst.FlowReturn.OK:
                    raise RuntimeError("Video encoder rejected a frame")
                self.frames_written += 1
            self._set_state("finalizing")
            vsrc.emit("end-of-stream")
            # EOS each source downstream without a competing bus signal watch.
            for i in range(len(self._audio_devices)):
                source = pipeline.get_by_name(f"asrc_{i}")
                if self._audio_devices[i] in self._external_audio:
                    source.emit("end-of-stream")
                else:
                    source.send_event(Gst.Event.new_eos())
            if self._audio_devices:
                pipeline.get_by_name("asilence").send_event(Gst.Event.new_eos())
            message = bus.timed_pop_filtered(10 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
            if message is None:
                raise RuntimeError("Recording finalization timed out; the partial file was preserved")
            if message.type == Gst.MessageType.ERROR:
                err, _debug = message.parse_error()
                raise RuntimeError(err.message)
            pipeline.set_state(Gst.State.NULL)
            if not self.frames_written or os.path.getsize(path) == 0:
                raise RuntimeError("The encoder produced no media")
            with open(path, "rb") as media:
                os.fsync(media.fileno())
            success = True
        except Exception as exc:
            error = str(exc)
            log.exception("Recording failed")
        finally:
            with self._lock:
                self._pcm_sources.clear()
            for process, thread in self._audio_processes:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                thread.join(timeout=2)
                process.stdout.close()
            if pipeline is not None:
                pipeline.set_state(Gst.State.NULL)
            self._set_state("idle" if success else "error")
            self._done.set()
            GLib.idle_add(self._finalized, path, success, error)

    def _finalized(self, path, success, error):
        self.emit("finalized", path, success, error)
        return GLib.SOURCE_REMOVE
