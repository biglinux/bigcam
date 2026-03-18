"""Video recorder – GStreamer-based video recording with audio."""

from __future__ import annotations

import os
import time
import logging
import threading
from typing import Any

import gi

gi.require_version("Gst", "1.0")

from gi.repository import Gst, GLib

from core.camera_backend import CameraInfo
from core.camera_manager import CameraManager
from utils import xdg

log = logging.getLogger(__name__)
class VideoRecorder:
    """Records video+audio using a unified GStreamer pipeline fed by appsrc.

    This ensures that processed frames (with effects) from StreamEngine are
    captured correctly.
    """

    def __init__(self, camera_manager: CameraManager) -> None:
        self._manager = camera_manager
        self._recording = False
        self._output_path = ""
        self._pipeline: Gst.Pipeline | None = None
        self._vsrc: Gst.Element | None = None
        self._eos_received = threading.Event()
        self._w = 0
        self._h = 0
        self._start_time = 0

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def output_path(self) -> str:
        return self._output_path

    def start(
        self,
        camera: CameraInfo,
        pipeline: Gst.Pipeline | None = None,
        filename: str | None = None,
        mirror: bool = False,
        record_audio: bool = True,
    ) -> str | None:
        """Initialize recording. The actual pipeline starts on the first frame."""
        if self._recording:
            return None

        if filename is None:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"bigcam_{timestamp}.mkv"

        output_dir = xdg.videos_dir()
        os.makedirs(output_dir, exist_ok=True)
        self._output_path = os.path.join(output_dir, filename)
        self._record_audio = record_audio
        self._recording = True
        self._w = 0
        self._h = 0
        self._pipeline = None
        self._vsrc = None
        self._start_time = time.time()

        log.info("Recording initialized: %s", self._output_path)
        return self._output_path

    def _pick_encoder_str(self) -> str:
        """Return the encoder element string, trying hw first."""
        candidates = [
            ("vaapih264enc", "rate-control=2 bitrate=8000"),
            ("vah264enc", "rate-control=2 bitrate=8000"),
        ]
        for name, props in candidates:
            if Gst.ElementFactory.find(name):
                log.info("Using hardware encoder for recording: %s", name)
                return f"{name} {props} ! h264parse"

        # Software fallback
        log.info("Using software encoder for recording: x264enc")
        return "x264enc tune=4 speed-preset=3 bitrate=8000 key-int-max=60 bframes=0 threads=0 ! h264parse"

    def _ensure_pipeline(self, w: int, h: int) -> bool:
        if self._pipeline:
            return True

        self._w = w
        self._h = h
        enc_str = self._pick_encoder_str()
        audio_str = ""
        if self._record_audio:
            # We use a large queue for audio to prevent drops if video encoding is slow
            audio_str = "pulsesrc ! queue max-size-time=3000000000 ! audioconvert ! opusenc ! mux. "

        escaped = self._output_path.replace('"', '\\"')
        pipeline_str = (
            f"appsrc name=vsrc format=time is-live=true do-timestamp=true "
            f"caps=video/x-raw,format=BGR,width={w},height={h},framerate=30/1 ! "
            f"videoconvert ! {enc_str} ! "
            f"matroskamux name=mux ! filesink location=\"{escaped}\" "
            f"{audio_str}"
        )

        log.debug("Starting recording pipeline: %s", pipeline_str)
        try:
            self._pipeline = Gst.parse_launch(pipeline_str)
            self._vsrc = self._pipeline.get_by_name("vsrc")

            bus = self._pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message::eos", self._on_eos)
            bus.connect("message::error", self._on_error)
            self._eos_received.clear()

            ret = self._pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                log.error("Failed to start recording pipeline")
                self._stop_pipeline()
                return False
            return True
        except Exception as exc:
            log.error("Failed to create recording pipeline: %s", exc)
            return False

    def write_frame(self, bgr: Any) -> None:
        """Push a processed BGR frame into the recording pipeline."""
        if not self._recording:
            return

        h, w = bgr.shape[:2]
        if not self._ensure_pipeline(w, h):
            return

        data = bgr.tobytes()
        buf = Gst.Buffer.new_wrapped(data)
        # We let appsrc (do-timestamp=true) handle the timestamps relative to pipeline start
        if self._vsrc:
            ret = self._vsrc.emit("push-buffer", buf)
            if ret != Gst.FlowReturn.OK:
                log.warning("Recording appsrc push error: %s", ret)

    def _on_eos(self, _bus, _msg):
        log.info("Recording pipeline EOS")
        self._eos_received.set()

    def _on_error(self, _bus, msg):
        err, dbg = msg.parse_error()
        log.error("Recording pipeline error: %s (%s)", err.message, dbg)

    def stop(self) -> str | None:
        """Stop recording and finalize the file."""
        if not self._recording:
            return None

        self._recording = False
        path = self._output_path

        if self._pipeline:
            if self._vsrc:
                self._vsrc.emit("end-of-stream")
            
            # Wait for EOS to ensure file is finalized (especially for Matroska)
            if not self._eos_received.wait(timeout=3.0):
                log.warning("Recording stop: timeout waiting for EOS")
            
            self._stop_pipeline()

        log.info("Recording stopped: %s", path)
        return path

    def _stop_pipeline(self) -> None:
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        self._vsrc = None
