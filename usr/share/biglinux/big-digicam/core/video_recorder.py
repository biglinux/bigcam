"""Video recorder – GStreamer-based video recording via tee from preview pipeline."""

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
    """Records video by branching off the active preview pipeline tee."""

    def __init__(self, camera_manager: CameraManager) -> None:
        self._manager = camera_manager
        self._recording = False
        self._output_path = ""
        # Elements added to the preview pipeline for recording
        self._rec_queue: Gst.Element | None = None
        self._rec_convert: Gst.Element | None = None
        self._rec_encoder: Gst.Element | None = None
        self._rec_muxer: Gst.Element | None = None
        self._rec_sink: Gst.Element | None = None
        self._tee: Gst.Element | None = None
        self._tee_pad: Gst.Pad | None = None
        self._pipeline: Gst.Pipeline | None = None

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
    ) -> str | None:
        """Start recording by adding a branch to the preview pipeline tee."""
        if self._recording:
            return None

        if not pipeline:
            log.error("No pipeline provided for recording")
            return None

        tee = pipeline.get_by_name("t")
        if not tee:
            log.error("No tee element in pipeline — cannot record")
            return None

        if filename is None:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"bigcam_{timestamp}.mkv"

        output_dir = xdg.videos_dir()
        os.makedirs(output_dir, exist_ok=True)
        self._output_path = os.path.join(output_dir, filename)

        try:
            self._rec_queue = Gst.ElementFactory.make("queue", "rec_queue")
            self._rec_queue.set_property("max-size-buffers", 0)
            self._rec_queue.set_property("max-size-time", 0)
            self._rec_queue.set_property("max-size-bytes", 0)
            self._rec_convert = Gst.ElementFactory.make("videoconvert", "rec_convert")
            self._rec_encoder = Gst.ElementFactory.make("x264enc", "rec_encoder")
            self._rec_encoder.set_property("tune", 4)  # zerolatency
            self._rec_encoder.set_property("speed-preset", 1)  # ultrafast
            self._rec_muxer = Gst.ElementFactory.make("matroskamux", "rec_muxer")
            self._rec_sink = Gst.ElementFactory.make("filesink", "rec_sink")
            self._rec_sink.set_property("location", self._output_path)

            for elem in (self._rec_queue, self._rec_convert, self._rec_encoder, self._rec_muxer, self._rec_sink):
                if elem is None:
                    log.error("Failed to create recording element")
                    return None
                pipeline.add(elem)

            self._rec_queue.link(self._rec_convert)
            self._rec_convert.link(self._rec_encoder)
            self._rec_encoder.link(self._rec_muxer)
            self._rec_muxer.link(self._rec_sink)

            self._rec_queue.sync_state_with_parent()
            self._rec_convert.sync_state_with_parent()
            self._rec_encoder.sync_state_with_parent()
            self._rec_muxer.sync_state_with_parent()
            self._rec_sink.sync_state_with_parent()

            # Request a new src pad from tee and link to queue
            tee_src = tee.request_pad_simple("src_%u")
            queue_sink = self._rec_queue.get_static_pad("sink")
            tee_src.link(queue_sink)

            self._tee = tee
            self._tee_pad = tee_src
            self._pipeline = pipeline
            self._recording = True
            log.info("Recording started: %s", self._output_path)
            return self._output_path
        except Exception as exc:
            log.error("Failed to start recording: %s", exc)
            self._cleanup_elements()
            return None

    def stop(self) -> str | None:
        """Stop recording by removing the branch from the pipeline."""
        if not self._recording:
            return None

        self._recording = False
        output = self._output_path

        try:
            if self._tee_pad and self._tee and self._rec_queue:
                # Block the tee src pad, then detach in the callback
                self._tee_pad.add_probe(
                    Gst.PadProbeType.BLOCK_DOWNSTREAM,
                    self._on_tee_pad_blocked,
                )
                # Wait for the probe to fire and cleanup to complete
                time.sleep(0.5)
            else:
                self._cleanup_elements()
        except Exception as exc:
            log.warning("Error stopping recording: %s", exc)
            self._cleanup_elements()

        log.info("Recording stopped: %s", output)
        return output

    def _on_tee_pad_blocked(
        self, pad: Gst.Pad, info: Gst.PadProbeInfo
    ) -> Gst.PadProbeReturn:
        """Called when the tee pad is blocked — safely detach recording branch."""
        try:
            # Send EOS only to the recording branch queue
            if self._rec_queue:
                queue_sink = self._rec_queue.get_static_pad("sink")
                pad.unlink(queue_sink)

            if self._tee:
                self._tee.release_request_pad(pad)

            # Set recording elements to NULL and remove from pipeline
            self._cleanup_elements()
        except Exception as exc:
            log.warning("Error in pad blocked callback: %s", exc)
            self._cleanup_elements()

        return Gst.PadProbeReturn.REMOVE

    def _cleanup_elements(self) -> None:
        """Remove recording elements from the pipeline."""
        for elem in (self._rec_sink, self._rec_muxer, self._rec_encoder, self._rec_convert, self._rec_queue):
            if elem and self._pipeline:
                elem.set_state(Gst.State.NULL)
                self._pipeline.remove(elem)
        self._rec_queue = None
        self._rec_convert = None
        self._rec_encoder = None
        self._rec_muxer = None
        self._rec_sink = None
        self._tee = None
        self._tee_pad = None
        self._pipeline = None

    def _on_error(self, _bus: Gst.Bus, msg: Gst.Message) -> None:
        err, dbg = msg.parse_error()
        log.error("Recording error: %s (debug: %s)", err.message, dbg)
        self.stop()
