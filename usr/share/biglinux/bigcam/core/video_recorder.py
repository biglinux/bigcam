"""Video recorder – GStreamer-based video recording with audio."""

from __future__ import annotations

import os
import shlex
import time
import logging
import threading
from typing import Any

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstApp", "1.0")

from gi.repository import Gst, GLib

from core.camera_backend import CameraInfo
from core.camera_manager import CameraManager
from utils import xdg

log = logging.getLogger(__name__)


class VideoRecorder:
    """Records video+audio using a separate GStreamer pipeline.

    Video frames are captured via an appsink tee branch from the preview pipeline
    and pushed into an appsrc in an isolated recording pipeline.
    Audio is captured directly from PulseAudio/PipeWire via pulsesrc.
    """

    def __init__(self, camera_manager: CameraManager) -> None:
        self._manager = camera_manager
        self._recording = False
        self._output_path = ""
        # Preview pipeline tee branch (appsink)
        self._appsink: Gst.Element | None = None
        self._appsink_queue: Gst.Element | None = None
        self._appsink_convert: Gst.Element | None = None
        self._appsink_flip: Gst.Element | None = None
        self._tee: Gst.Element | None = None
        self._tee_pad: Gst.Pad | None = None
        self._preview_pipeline: Gst.Pipeline | None = None
        # Separate recording pipeline
        self._rec_pipeline: Gst.Pipeline | None = None
        self._appsrc: Gst.Element | None = None
        self._eos_received = threading.Event()
        self._base_pts: int | None = None  # for rebasing video timestamps
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
        record_audio: bool = True,
    ) -> str | None:
        """Start recording video+audio.

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
            # 1. Add appsink branch to preview pipeline's tee
            if not self._setup_appsink_branch(pipeline, tee, mirror):
                return None

            # 2. Reset timestamp base for new recording
            self._base_pts = None

            # 3. Create separate recording pipeline (appsrc + pulsesrc → muxer → filesink)
            if not self._create_recording_pipeline(record_audio):
                self._remove_appsink_branch()
                return None

            # 4. Start recording pipeline
            ret = self._rec_pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                log.error("Failed to start recording pipeline")
                self._destroy_recording_pipeline()
                self._remove_appsink_branch()
                return None

            self._recording = True
            log.info("Recording started: %s", self._output_path)
            return self._output_path
        except Exception as exc:
            log.error("Failed to start recording: %s", exc)
            self._destroy_recording_pipeline()
            self._remove_appsink_branch()
            return None

    def _setup_appsink_branch(
        self, pipeline: Gst.Pipeline, tee: Gst.Element, mirror: bool
    ) -> bool:
        """Add appsink branch to the preview pipeline's tee to capture video frames."""
        self._appsink_queue = Gst.ElementFactory.make("queue", "rec_appsink_queue")
        self._appsink_queue.set_property("max-size-buffers", 3)
        self._appsink_queue.set_property("leaky", 2)  # drop old buffers
        self._appsink_convert = Gst.ElementFactory.make(
            "videoconvert", "rec_appsink_convert"
        )
        self._appsink = Gst.ElementFactory.make("appsink", "rec_appsink")
        self._appsink.set_property("emit-signals", True)
        self._appsink.set_property("drop", True)
        self._appsink.set_property("max-buffers", 2)
        # Output I420 for x264enc compatibility
        caps = Gst.Caps.from_string("video/x-raw,format=I420")
        self._appsink.set_property("caps", caps)
        self._appsink.connect("new-sample", self._on_new_sample)

        self._appsink_flip = None
        if mirror:
            self._appsink_flip = Gst.ElementFactory.make(
                "videoflip", "rec_appsink_flip"
            )
            if self._appsink_flip:
                self._appsink_flip.set_property("method", "horizontal-flip")

        elems = [self._appsink_queue, self._appsink_convert, self._appsink]
        if self._appsink_flip:
            elems.insert(1, self._appsink_flip)

        for elem in elems:
            if elem is None:
                log.error("Failed to create appsink element")
                return False
            pipeline.add(elem)

        # Link: queue → [flip →] convert → appsink
        if self._appsink_flip:
            self._appsink_queue.link(self._appsink_flip)
            self._appsink_flip.link(self._appsink_convert)
        else:
            self._appsink_queue.link(self._appsink_convert)
        self._appsink_convert.link(self._appsink)

        # Link tee → queue
        tee_src = tee.request_pad_simple("src_%u")
        queue_sink = self._appsink_queue.get_static_pad("sink")
        tee_src.link(queue_sink)

        for elem in elems:
            elem.sync_state_with_parent()

        self._tee = tee
        self._tee_pad = tee_src
        self._preview_pipeline = pipeline
        return True

    def _create_recording_pipeline(
        self, record_audio: bool, *, _encoder_idx: int = 0
    ) -> bool:
        """Create a separate GStreamer pipeline for recording."""
        audio_branch = ""
        if record_audio:
            audio_branch = "pulsesrc ! queue ! audioconvert ! opusenc ! mux. "

        encoders = self._available_encoders()
        if _encoder_idx >= len(encoders):
            if record_audio:
                log.warning("Retrying without audio")
                return self._create_recording_pipeline(
                    record_audio=False, _encoder_idx=0
                )
            log.error("No working video encoder found")
            return False

        video_enc = encoders[_encoder_idx]
        log.info("Trying encoder [%d/%d]: %s", _encoder_idx + 1, len(encoders), video_enc)

        pipeline_str = (
            "appsrc name=videosrc format=time is-live=true ! "
            "queue max-size-buffers=10 max-size-time=0 max-size-bytes=0 ! "
            "videoconvert ! videoscale ! "
            "video/x-raw,pixel-aspect-ratio=1/1 ! "
            f"{video_enc} ! h264parse ! "
            "mux. "
            f"{audio_branch}"
            f"matroskamux name=mux ! filesink location={shlex.quote(self._output_path)}"
        )

        try:
            self._rec_pipeline = Gst.parse_launch(pipeline_str)
        except GLib.Error as exc:
            log.warning("Pipeline parse failed (%s): %s", video_enc.split()[0], exc.message)
            return self._create_recording_pipeline(
                record_audio, _encoder_idx=_encoder_idx + 1
            )

        self._appsrc = self._rec_pipeline.get_by_name("videosrc")
        if not self._appsrc:
            log.error("Failed to get appsrc from recording pipeline")
            return False

        # Watch for EOS on the recording pipeline
        bus = self._rec_pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", self._on_rec_eos)
        bus.connect("message::error", self._on_rec_error)

        self._eos_received.clear()
        return True

    @staticmethod
    def _available_encoders() -> list[str]:
        """Return a list of encoder pipeline strings in preference order."""
        candidates = [
            ("vaapih264enc", "vaapih264enc rate-control=cbr bitrate=6000"),
            ("vah264enc", "vah264enc rate-control=cbr bitrate=6000"),
            (
                "openh264enc",
                "openh264enc complexity=0 bitrate=6000000 rate-control=bitrate",
            ),
        ]
        result = []
        for name, enc_str in candidates:
            if Gst.ElementFactory.find(name) is not None:
                result.append(enc_str)
        # x264enc as final fallback — always available
        result.append(
            "x264enc tune=zerolatency speed-preset=ultrafast "
            "bitrate=6000 key-int-max=30 b-adapt=false bframes=0 threads=0"
        )
        return result

    def _on_new_sample(self, appsink: Gst.Element) -> Gst.FlowReturn:
        """Callback: push video frame from preview appsink to recording appsrc."""
        if not self._recording or not self._appsrc:
            return Gst.FlowReturn.OK

        sample = appsink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK

        buf = sample.get_buffer()
        caps = sample.get_caps()

        # Rebase PTS to start from 0 so video aligns with audio from pulsesrc
        buf = buf.copy()
        pts = buf.pts
        if pts != Gst.CLOCK_TIME_NONE:
            if self._base_pts is None:
                self._base_pts = pts
            buf.pts = pts - self._base_pts
            buf.dts = Gst.CLOCK_TIME_NONE

        # Set caps on appsrc if not already set
        current_caps = self._appsrc.get_property("caps")
        if current_caps is None or not current_caps.is_equal(caps):
            self._appsrc.set_property("caps", caps)

        ret = self._appsrc.emit("push-buffer", buf)
        if ret != Gst.FlowReturn.OK:
            log.debug("appsrc push-buffer returned %s", ret)

        return Gst.FlowReturn.OK

    def _on_rec_eos(self, _bus: Gst.Bus, _msg: Gst.Message) -> None:
        """Recording pipeline received EOS — file is finalized."""
        log.info("Recording pipeline EOS received")
        self._eos_received.set()

    def _on_rec_error(self, _bus: Gst.Bus, msg: Gst.Message) -> None:
        """Recording pipeline error."""
        err, dbg = msg.parse_error()
        log.error("Recording pipeline error: %s (debug: %s)", err.message, dbg)

    def stop(self) -> str | None:
        """Stop recording."""
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
            # 1. Send EOS to appsrc to finalize the recording pipeline
            if self._appsrc:
                self._appsrc.emit("end-of-stream")

            # 2. Wait for EOS to propagate through the recording pipeline
            if not self._eos_received.wait(timeout=5.0):
                log.warning("Timeout waiting for recording EOS")

            # 3. Stop and destroy the recording pipeline
            self._destroy_recording_pipeline()

            # 4. Remove appsink branch from preview pipeline
            self._remove_appsink_branch()
        except Exception as exc:
            log.warning("Error stopping recording: %s", exc)
            self._destroy_recording_pipeline()
            self._remove_appsink_branch()

        log.info("Recording stopped: %s", output)
        return output

    def _destroy_recording_pipeline(self) -> None:
        """Set recording pipeline to NULL and clean up."""
        if self._rec_pipeline:
            self._rec_pipeline.set_state(Gst.State.NULL)
            bus = self._rec_pipeline.get_bus()
            if bus:
                bus.remove_signal_watch()
            self._rec_pipeline = None
        self._appsrc = None

    def _remove_appsink_branch(self) -> None:
        """Remove appsink branch from the preview pipeline."""
        if self._tee_pad and self._tee:
            if self._appsink_queue:
                queue_sink = self._appsink_queue.get_static_pad("sink")
                self._tee_pad.unlink(queue_sink)
            self._tee.release_request_pad(self._tee_pad)

        for elem in (
            self._appsink,
            self._appsink_convert,
            self._appsink_flip,
            self._appsink_queue,
        ):
            if elem and self._preview_pipeline:
                elem.set_state(Gst.State.NULL)
                self._preview_pipeline.remove(elem)

        self._appsink = None
        self._appsink_queue = None
        self._appsink_convert = None
        self._appsink_flip = None
        self._tee = None
        self._tee_pad = None
        self._preview_pipeline = None

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
