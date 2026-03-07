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
    """Records video+audio using a hybrid pipeline approach.

    Video encoding (x264enc) happens directly in the preview pipeline tee branch,
    eliminating raw-video buffer copies and frame drops. The encoded h264 stream
    is bridged via a lightweight appsink→appsrc into a separate muxing pipeline
    that also captures audio from PulseAudio/PipeWire.
    """

    def __init__(self, camera_manager: CameraManager) -> None:
        self._manager = camera_manager
        self._recording = False
        self._output_path = ""
        # Encoder branch in preview pipeline
        self._enc_queue: Gst.Element | None = None
        self._enc_convert: Gst.Element | None = None
        self._enc_scale: Gst.Element | None = None
        self._enc_capsfilter: Gst.Element | None = None
        self._enc_encoder: Gst.Element | None = None
        self._enc_parse: Gst.Element | None = None
        self._enc_appsink: Gst.Element | None = None
        self._enc_flip: Gst.Element | None = None
        # Tee connection
        self._tee: Gst.Element | None = None
        self._tee_pad: Gst.Pad | None = None
        self._preview_pipeline: Gst.Pipeline | None = None
        # Muxing pipeline (separate)
        self._mux_pipeline: Gst.Pipeline | None = None
        self._mux_appsrc: Gst.Element | None = None
        self._eos_received = threading.Event()
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
            # 1. Add encoder branch to preview pipeline tee
            if not self._setup_encoder_branch(pipeline, tee, mirror):
                return None

            # 2. Create separate muxing pipeline (appsrc + pulsesrc → mux → filesink)
            if not self._create_mux_pipeline(record_audio):
                self._teardown_encoder_branch()
                return None

            # 3. Start muxing pipeline
            ret = self._mux_pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                log.error("Failed to start muxing pipeline")
                self._destroy_mux_pipeline()
                self._teardown_encoder_branch()
                return None

            self._recording = True
            log.info("Recording started: %s", self._output_path)
            return self._output_path
        except Exception as exc:
            log.error("Failed to start recording: %s", exc)
            self._destroy_mux_pipeline()
            self._teardown_encoder_branch()
            return None

    def _pick_encoder(self) -> tuple[Gst.Element, Gst.Element] | None:
        """Create and return (encoder, h264parse) elements, trying hw first."""
        candidates = [
            ("vaapih264enc", {"rate-control": 2, "bitrate": 8000}),
            ("vah264enc", {"rate-control": 2, "bitrate": 8000}),
        ]
        for name, props in candidates:
            factory = Gst.ElementFactory.find(name)
            if factory is not None:
                enc = factory.create(None)
                if enc:
                    for k, v in props.items():
                        enc.set_property(k, v)
                    parse = Gst.ElementFactory.make("h264parse", None)
                    log.info("Using hardware encoder: %s", name)
                    return (enc, parse)
        # Software fallback: x264enc
        enc = Gst.ElementFactory.make("x264enc", None)
        if enc is None:
            return None
        enc.set_property("tune", 4)              # zerolatency
        enc.set_property("speed-preset", 3)      # veryfast
        enc.set_property("bitrate", 8000)
        enc.set_property("key-int-max", 60)
        enc.set_property("bframes", 0)
        enc.set_property("threads", 0)
        parse = Gst.ElementFactory.make("h264parse", None)
        log.info("Using software encoder: x264enc")
        return (enc, parse)

    def _setup_encoder_branch(
        self,
        pipeline: Gst.Pipeline,
        tee: Gst.Element,
        mirror: bool,
    ) -> bool:
        """Add encoder branch to preview tee: queue → convert → x264enc → h264parse → appsink."""
        enc_pair = self._pick_encoder()
        if enc_pair is None:
            log.error("No H.264 encoder available")
            return False
        encoder, h264parse = enc_pair

        queue = Gst.ElementFactory.make("queue", "rec_enc_queue")
        queue.set_property("max-size-buffers", 30)
        queue.set_property("max-size-time", 0)
        queue.set_property("max-size-bytes", 0)
        queue.set_property("leaky", 2)

        convert = Gst.ElementFactory.make("videoconvert", "rec_enc_convert")
        scale = Gst.ElementFactory.make("videoscale", "rec_enc_scale")
        capsfilter = Gst.ElementFactory.make("capsfilter", "rec_enc_caps")
        capsfilter.set_property(
            "caps",
            Gst.Caps.from_string("video/x-raw,format=I420,pixel-aspect-ratio=1/1"),
        )

        appsink = Gst.ElementFactory.make("appsink", "rec_enc_appsink")
        appsink.set_property("emit-signals", True)
        appsink.set_property("drop", False)
        appsink.set_property("max-buffers", 60)
        appsink.set_property("sync", False)
        appsink.connect("new-sample", self._on_encoded_sample)

        flip = None
        if mirror:
            flip = Gst.ElementFactory.make("videoflip", "rec_enc_flip")
            if flip:
                flip.set_property("method", "horizontal-flip")

        # Build element list
        elems = [queue]
        if flip:
            elems.append(flip)
        elems.extend([convert, scale, capsfilter, encoder, h264parse, appsink])

        for elem in elems:
            if elem is None:
                log.error("Failed to create encoder element")
                return False
            pipeline.add(elem)

        # Link chain
        prev = elems[0]
        for elem in elems[1:]:
            if not prev.link(elem):
                log.error("Failed to link %s → %s", prev.get_name(), elem.get_name())
                for e in elems:
                    pipeline.remove(e)
                return False
            prev = elem

        # Sync elements to PLAYING before connecting to tee
        for elem in elems:
            elem.sync_state_with_parent()

        # Connect tee → queue
        tee_src = tee.request_pad_simple("src_%u")
        queue_sink = queue.get_static_pad("sink")
        link_ret = tee_src.link(queue_sink)
        log.info("Tee → encoder queue link: %s", link_ret)

        self._enc_queue = queue
        self._enc_convert = convert
        self._enc_scale = scale
        self._enc_capsfilter = capsfilter
        self._enc_encoder = encoder
        self._enc_parse = h264parse
        self._enc_appsink = appsink
        self._enc_flip = flip
        self._tee = tee
        self._tee_pad = tee_src
        self._preview_pipeline = pipeline
        return True

    def _create_mux_pipeline(self, record_audio: bool) -> bool:
        """Create separate muxing pipeline: appsrc (h264) + pulsesrc → mux → filesink."""
        audio_branch = ""
        if record_audio:
            audio_branch = "pulsesrc ! queue max-size-time=3000000000 ! audioconvert ! opusenc ! mux. "

        pipeline_str = (
            "appsrc name=videosrc format=time is-live=true do-timestamp=false ! "
            "queue max-size-buffers=60 max-size-time=0 max-size-bytes=0 ! "
            "h264parse ! "
            "mux. "
            f"{audio_branch}"
            f"matroskamux name=mux streamable=true ! "
            f"filesink location={GLib.shell_quote(self._output_path)}"
        )

        try:
            self._mux_pipeline = Gst.parse_launch(pipeline_str)
        except GLib.Error as exc:
            log.error("Mux pipeline parse failed: %s", exc.message)
            if record_audio:
                log.warning("Retrying mux pipeline without audio")
                return self._create_mux_pipeline(record_audio=False)
            return False

        self._mux_appsrc = self._mux_pipeline.get_by_name("videosrc")
        if not self._mux_appsrc:
            log.error("Failed to get appsrc from mux pipeline")
            return False

        # Watch for EOS and errors
        bus = self._mux_pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::eos", self._on_mux_eos)
        bus.connect("message::error", self._on_mux_error)
        self._eos_received.clear()

        return True

    def _on_encoded_sample(self, appsink: Gst.Element) -> Gst.FlowReturn:
        """Forward encoded h264 sample from preview appsink to mux appsrc."""
        if not self._recording or not self._mux_appsrc:
            return Gst.FlowReturn.OK

        sample = appsink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK

        buf = sample.get_buffer()
        caps = sample.get_caps()

        # Set caps on appsrc if needed
        current_caps = self._mux_appsrc.get_property("caps")
        if current_caps is None or not current_caps.is_equal(caps):
            self._mux_appsrc.set_property("caps", caps)

        ret = self._mux_appsrc.emit("push-buffer", buf)
        if ret != Gst.FlowReturn.OK:
            log.debug("mux appsrc push-buffer: %s", ret)

        return Gst.FlowReturn.OK

    def _on_mux_eos(self, _bus: Gst.Bus, _msg: Gst.Message) -> None:
        log.info("Muxing pipeline EOS — file finalized")
        self._eos_received.set()

    def _on_mux_error(self, _bus: Gst.Bus, msg: Gst.Message) -> None:
        err, dbg = msg.parse_error()
        log.error("Muxing pipeline error: %s (debug: %s)", err.message, dbg)

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
            # 1. Send EOS to mux appsrc to finalize the video stream
            if self._mux_appsrc:
                self._mux_appsrc.emit("end-of-stream")

            # 2. Wait for mux pipeline to finalize the file
            if not self._eos_received.wait(timeout=5.0):
                log.warning("Timeout waiting for mux EOS")

            # 3. Destroy mux pipeline
            self._destroy_mux_pipeline()

            # 4. Remove encoder branch from preview pipeline
            self._teardown_encoder_branch()
        except Exception as exc:
            log.warning("Error stopping recording: %s", exc)
            self._destroy_mux_pipeline()
            self._teardown_encoder_branch()

        log.info("Recording stopped: %s", output)
        return output

    def _destroy_mux_pipeline(self) -> None:
        """Stop and clean up the muxing pipeline."""
        if self._mux_pipeline:
            self._mux_pipeline.set_state(Gst.State.NULL)
            bus = self._mux_pipeline.get_bus()
            if bus:
                bus.remove_signal_watch()
            self._mux_pipeline = None
        self._mux_appsrc = None

    def _teardown_encoder_branch(self) -> None:
        """Remove encoder branch from the preview pipeline."""
        pipeline = self._preview_pipeline
        if not pipeline:
            return

        # Disconnect tee pad
        if self._tee_pad and self._tee and self._enc_queue:
            queue_sink = self._enc_queue.get_static_pad("sink")
            if queue_sink:
                self._tee_pad.unlink(queue_sink)
            self._tee.release_request_pad(self._tee_pad)

        all_elems = [
            self._enc_appsink,
            self._enc_parse,
            self._enc_encoder,
            self._enc_capsfilter,
            self._enc_scale,
            self._enc_convert,
            self._enc_flip,
            self._enc_queue,
        ]

        for elem in all_elems:
            if elem is not None:
                elem.set_state(Gst.State.NULL)
                pipeline.remove(elem)

        self._enc_queue = None
        self._enc_convert = None
        self._enc_scale = None
        self._enc_capsfilter = None
        self._enc_encoder = None
        self._enc_parse = None
        self._enc_appsink = None
        self._enc_flip = None
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
