"""Video recorder – GStreamer-based video recording."""

from __future__ import annotations

import os
import time
import logging

import gi

gi.require_version("Gst", "1.0")

from gi.repository import Gst, GLib

from core.camera_backend import CameraInfo
from core.camera_manager import CameraManager
from utils import xdg
from utils.i18n import _

log = logging.getLogger(__name__)


class VideoRecorder:
    """Records video from the active camera to a file."""

    def __init__(self, camera_manager: CameraManager) -> None:
        self._manager = camera_manager
        self._pipeline: Gst.Pipeline | None = None
        self._recording = False
        self._output_path = ""

    @property
    def is_recording(self) -> bool:
        return self._recording

    @property
    def output_path(self) -> str:
        return self._output_path

    def start(self, camera: CameraInfo, filename: str | None = None) -> str | None:
        """Start recording. Returns the output path on success, None otherwise."""
        if self._recording:
            return None

        gst_source = self._manager.get_gst_source(camera)
        if not gst_source:
            return None

        if filename is None:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"bigcam_{timestamp}.mkv"

        output_dir = xdg.videos_dir()
        os.makedirs(output_dir, exist_ok=True)
        self._output_path = os.path.join(output_dir, filename)

        pipeline_str = (
            f"{gst_source} ! queue ! videoconvert ! "
            f"x264enc tune=zerolatency speed-preset=ultrafast ! "
            f'matroskamux ! filesink location="{self._output_path}"'
        )

        try:
            self._pipeline = Gst.parse_launch(pipeline_str)
            bus = self._pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message::error", self._on_error)

            ret = self._pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                self._pipeline = None
                return None
            self._recording = True
            return self._output_path
        except Exception as exc:
            log.error("Failed to start recording: %s", exc)
            return None

    def stop(self) -> str | None:
        """Stop recording. Returns the output path."""
        if not self._recording or not self._pipeline:
            return None

        self._pipeline.send_event(Gst.Event.new_eos())
        # Wait briefly for EOS to propagate
        self._pipeline.get_state(Gst.CLOCK_TIME_NONE // 10)  # ~100ms
        self._pipeline.set_state(Gst.State.NULL)
        self._pipeline = None
        self._recording = False
        return self._output_path

    def _on_error(self, _bus: Gst.Bus, msg: Gst.Message) -> None:
        err, dbg = msg.parse_error()
        log.error("Recording error: %s (debug: %s)", err.message, dbg)
        self.stop()
