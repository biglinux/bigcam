"""Video recorder – GStreamer-based video recording via tee from preview pipeline."""

from __future__ import annotations

import os
import time
import logging
from typing import Any

import gi

gi.require_version("Gst", "1.0")

from gi.repository import Gst  # noqa: E402

from core.camera_backend import CameraInfo  # noqa: E402
from core.camera_manager import CameraManager  # noqa: E402
from utils import xdg  # noqa: E402

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
        self._rec_flip: Gst.Element | None = None
        self._rec_encoder: Gst.Element | None = None
        self._rec_muxer: Gst.Element | None = None
        self._rec_sink: Gst.Element | None = None
        self._tee: Gst.Element | None = None
        self._tee_pad: Gst.Pad | None = None
        self._pipeline: Gst.Pipeline | None = None
        # OpenCV fallback for phone camera
        self._cv_writer: Any = None
        self._cv_mode = False
        self._cv_first_frame = True

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
    ) -> str | None:
        """Start recording by adding a branch to the preview pipeline tee.

        For phone cameras (no GStreamer pipeline), falls back to OpenCV VideoWriter.
        """
        if self._recording:
            return None

        if filename is None:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"bigcam_{timestamp}.mkv"

        output_dir = xdg.videos_dir()
        os.makedirs(output_dir, exist_ok=True)
        self._output_path = os.path.join(output_dir, filename)

        # Phone camera: use OpenCV VideoWriter fallback
        from constants import BackendType

        if camera.backend == BackendType.PHONE:
            return self._start_cv_recording(camera)

        if not pipeline:
            log.error("No pipeline provided for recording")
            return None

        tee = pipeline.get_by_name("t")
        if not tee:
            log.error("No tee element in pipeline — cannot record")
            return None

        try:
            self._rec_queue = Gst.ElementFactory.make("queue", "rec_queue")
            self._rec_queue.set_property("max-size-buffers", 0)
            self._rec_queue.set_property("max-size-time", 0)
            self._rec_queue.set_property("max-size-bytes", 0)
            self._rec_convert = Gst.ElementFactory.make("videoconvert", "rec_convert")
            self._rec_flip = None
            if mirror:
                self._rec_flip = Gst.ElementFactory.make("videoflip", "rec_flip")
                if self._rec_flip:
                    self._rec_flip.set_property("method", "horizontal-flip")
            self._rec_encoder = Gst.ElementFactory.make("x264enc", "rec_encoder")
            self._rec_encoder.set_property("tune", 4)  # zerolatency
            self._rec_encoder.set_property("speed-preset", 1)  # ultrafast
            self._rec_muxer = Gst.ElementFactory.make("matroskamux", "rec_muxer")
            self._rec_sink = Gst.ElementFactory.make("filesink", "rec_sink")
            self._rec_sink.set_property("location", self._output_path)

            elements = [
                self._rec_queue,
                self._rec_convert,
                self._rec_encoder,
                self._rec_muxer,
                self._rec_sink,
            ]
            if self._rec_flip:
                elements.insert(1, self._rec_flip)  # after queue, before convert
            for elem in elements:
                if elem is None:
                    log.error("Failed to create recording element")
                    return None
                pipeline.add(elem)

            # Link: queue → [flip →] convert → encoder → muxer → sink
            if self._rec_flip:
                self._rec_queue.link(self._rec_flip)
                self._rec_flip.link(self._rec_convert)
            else:
                self._rec_queue.link(self._rec_convert)
            self._rec_convert.link(self._rec_encoder)
            self._rec_encoder.link(self._rec_muxer)
            self._rec_muxer.link(self._rec_sink)

            for elem in elements:
                elem.sync_state_with_parent()

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

    # -- OpenCV fallback for phone camera ------------------------------------

    def _start_cv_recording(self, camera: CameraInfo) -> str | None:
        """Start recording using cv2.VideoWriter (for phone camera without GStreamer pipeline)."""
        try:
            import cv2
        except ImportError:
            log.error("OpenCV required for phone camera recording")
            return None

        # Use MJPG in MKV to match what we receive (JPEG frames)
        fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        # Default to 30fps; actual fps depends on phone capture rate
        self._cv_writer = cv2.VideoWriter(self._output_path, fourcc, 30, (0, 0))
        if not self._cv_writer.isOpened():
            # Defer opening until first frame (need resolution)
            self._cv_writer.release()
            self._cv_writer = None
        self._cv_mode = True
        self._cv_first_frame = True
        self._recording = True
        log.info("Recording (OpenCV) started: %s", self._output_path)
        return self._output_path

    def write_frame(self, bgr) -> None:
        """Write a BGR frame to the OpenCV video writer (phone camera recording)."""
        if not self._recording or not self._cv_mode:
            return
        import cv2

        h, w = bgr.shape[:2]
        if self._cv_first_frame or self._cv_writer is None:
            fourcc = cv2.VideoWriter_fourcc(*"MJPG")
            self._cv_writer = cv2.VideoWriter(self._output_path, fourcc, 30, (w, h))
            self._cv_first_frame = False
        if self._cv_writer and self._cv_writer.isOpened():
            self._cv_writer.write(bgr)

    def stop(self) -> str | None:
        """Stop recording by removing the branch from the pipeline."""
        if not self._recording:
            return None

        self._recording = False
        output = self._output_path

        if self._cv_mode:
            if self._cv_writer:
                self._cv_writer.release()
                self._cv_writer = None
            self._cv_mode = False
            log.info("Recording (OpenCV) stopped: %s", output)
            return output

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
        # Release the tee request pad to prevent leaks
        if self._tee_pad and self._tee:
            try:
                self._tee.release_request_pad(self._tee_pad)
            except Exception:
                log.debug("Ignored exception", exc_info=True)
        for elem in (
            self._rec_sink,
            self._rec_muxer,
            self._rec_encoder,
            self._rec_convert,
            self._rec_flip,
            self._rec_queue,
        ):
            if elem and self._pipeline:
                elem.set_state(Gst.State.NULL)
                self._pipeline.remove(elem)
        self._rec_queue = None
        self._rec_convert = None
        self._rec_flip = None
        self._rec_encoder = None
        self._rec_muxer = None
        self._rec_sink = None
        self._tee = None
        self._tee_pad = None
        self._pipeline = None
