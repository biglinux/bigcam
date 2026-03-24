"""Video recorder – GStreamer-based video recording with audio."""

from __future__ import annotations

import os
import subprocess
import time
import logging
import threading
from typing import Any

try:
    import cv2
    _HAS_CV2 = True
except ImportError:
    _HAS_CV2 = False

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
        self._audio_srcs: list[Gst.Element] = []
        self._audio_vol_elements: dict[str, Gst.Element] = {}
        self._global_muted: bool = False
        self._w = 0
        self._h = 0
        self._start_time = 0
        # Configurable codec/container/bitrate
        self._video_codec = "h264"
        self._audio_codec = "opus"
        self._container = "mkv"
        self._video_bitrate = 8000

    def configure(
        self,
        video_codec: str = "h264",
        audio_codec: str = "opus",
        container: str = "mkv",
        video_bitrate: int = 8000,
    ) -> None:
        """Set recording codec/container/bitrate preferences."""
        self._video_codec = video_codec
        self._audio_codec = audio_codec
        self._container = container
        self._video_bitrate = max(500, min(50000, video_bitrate))

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
        audio_sources: list[str] | None = None,
        active_audio_sources: list[str] | None = None,
        source_volumes: dict[str, float] | None = None,
        muted: bool = False,
    ) -> str | None:
        """Initialize recording. The actual pipeline starts on the first frame.

        Args:
            audio_sources: All PulseAudio source device names from AudioMonitor
                           to include in the recording pipeline.
            active_audio_sources: Subset of audio_sources that are currently
                                   active (unmuted). Others start muted.
            source_volumes: Per-source volume levels {source_name: 0.0–1.0}.
            muted: Whether global mute is currently active.
        """
        if self._recording:
            return None

        if filename is None:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            ext = self._container_ext()
            filename = f"bigcam_{timestamp}{ext}"

        output_dir = xdg.videos_dir()
        os.makedirs(output_dir, exist_ok=True)
        self._output_path = os.path.join(output_dir, filename)
        self._record_audio = record_audio
        self._audio_source_devices = audio_sources or []
        self._active_audio_set = set(active_audio_sources or [])
        self._source_volumes: dict[str, float] = dict(source_volumes or {})
        self._global_muted = muted
        self._recording = True
        self._w = 0
        self._h = 0
        self._pipeline = None
        self._vsrc = None
        self._audio_srcs = []
        self._audio_vol_elements = {}
        self._start_time = time.time()

        log.info("Recording initialized: %s (muted=%s)", self._output_path, muted)
        return self._output_path

    def _pick_encoder_str(self) -> str:
        """Return the encoder element string based on configured codec."""
        br = self._video_bitrate
        codec = self._video_codec

        # WebM only supports VP8/VP9
        if self._container == "webm" and codec != "vp9":
            log.info("Container webm requires VP9; overriding codec %s", codec)
            codec = "vp9"
        # MP4 doesn't support VP9 or MJPEG
        elif self._container == "mp4" and codec in ("vp9", "mjpeg"):
            log.info("Container mp4 incompatible with %s; falling back to h264", codec)
            codec = "h264"

        if codec == "h265":
            hw = [
                ("vaapih265enc", f"rate-control=2 bitrate={br}"),
                ("vah265enc", f"rate-control=2 bitrate={br}"),
            ]
            for name, props in hw:
                if Gst.ElementFactory.find(name):
                    log.info("Using hardware H.265 encoder: %s", name)
                    return f"{name} {props} ! h265parse"
            log.info("Using software H.265 encoder: x265enc")
            return f"x265enc bitrate={br} speed-preset=3 ! h265parse"

        if codec == "vp9":
            log.info("Using VP9 encoder: vp9enc")
            return f"vp9enc target-bitrate={br * 1000} cpu-used=4 deadline=1 threads=4"

        if codec == "mjpeg":
            log.info("Using MJPEG encoder: jpegenc")
            return "jpegenc quality=90"

        # Default: H.264
        hw = [
            ("vaapih264enc", f"rate-control=2 bitrate={br}"),
            ("vah264enc", f"rate-control=2 bitrate={br}"),
        ]
        for name, props in hw:
            if Gst.ElementFactory.find(name):
                log.info("Using hardware H.264 encoder: %s", name)
                return f"{name} {props} ! h264parse"
        log.info("Using software H.264 encoder: x264enc")
        return f"x264enc tune=4 speed-preset=3 bitrate={br} key-int-max=60 bframes=0 threads=0 ! h264parse"

    def _pick_audio_encoder_str(self) -> str:
        """Return the audio encoder element string based on configured codec."""
        codec = self._audio_codec

        # WebM only supports Opus/Vorbis
        if self._container == "webm" and codec not in ("opus", "vorbis"):
            log.info("Container webm requires Opus/Vorbis; overriding audio %s", codec)
            codec = "opus"
        # MP4 doesn't support Vorbis
        elif self._container == "mp4" and codec == "vorbis":
            log.info("Container mp4 incompatible with vorbis; falling back to opus")
            codec = "opus"

        if codec == "aac":
            for name in ("fdkaacenc", "avenc_aac", "voaacenc"):
                if Gst.ElementFactory.find(name):
                    log.info("Using AAC encoder: %s", name)
                    return name
            log.warning("No AAC encoder found, falling back to opusenc")
            return "opusenc"
        if codec == "mp3":
            log.info("Using MP3 encoder: lamemp3enc")
            return "lamemp3enc"
        if codec == "vorbis":
            log.info("Using Vorbis encoder: vorbisenc")
            return "vorbisenc"
        # Default: Opus
        return "opusenc"

    def _pick_muxer_str(self) -> str:
        """Return the muxer element string based on configured container."""
        container = self._container
        if container == "webm":
            return "webmmux"
        if container == "mp4":
            return "mp4mux"
        return "matroskamux"

    def _container_ext(self) -> str:
        """Return file extension for the configured container."""
        return {"webm": ".webm", "mp4": ".mp4"}.get(self._container, ".mkv")

    def _ensure_pipeline(self, w: int, h: int) -> bool:
        if self._pipeline:
            return True

        self._w = w
        self._h = h
        enc_str = self._pick_encoder_str()
        audio_enc = self._pick_audio_encoder_str()
        muxer = self._pick_muxer_str()
        audio_str = ""
        if self._record_audio:
            extra_devs = self._audio_source_devices
            # Determine initial volume for the system mic (respects global mute)
            mic_vol = 0.0 if self._global_muted else 1.0

            # Pipeline setup: always use audiomixer to combine system mic + cameras
            # All sources use provide-clock=false to use system/global pipeline clock
            # audiomixer latency handles sync between sources
            audio_str = (
                "audiomixer name=amix latency=500000000 ! "
                "queue max-size-time=2000000000 leaky=downstream ! audioconvert ! "
                f"audioresample ! audiorate ! {audio_enc} ! mux. "
            )

            # System Microfone (Default source)
            # do-timestamp=true: use pipeline clock for timestamps
            # provide-clock=false: don't compete for clock master
            audio_str += (
                "pulsesrc do-timestamp=true provide-clock=false "
                "buffer-time=200000 latency-time=50000 "
                "name=asrc_mic ! "
                "queue max-size-time=1000000000 leaky=downstream ! "
                "audioconvert ! audioresample ! "
                f"volume name=avol_mic volume={mic_vol} ! amix. "
            )

            if extra_devs:
                for i, dev in enumerate(extra_devs):
                    safe = dev.replace('"', '\\"')
                    if dev in self._active_audio_set and not self._global_muted:
                        vol = self._source_volumes.get(dev, 1.0)
                    else:
                        vol = 0.0
                    
                    # USB sources follow global clock
                    audio_str += (
                        f'pulsesrc device="{safe}" do-timestamp=true '
                        f'provide-clock=false '
                        f'buffer-time=500000 latency-time=100000 '
                        f'name=asrc_{i} ! '
                        f'queue max-size-time=2000000000 max-size-buffers=0 max-size-bytes=0 ! '
                        f'audioconvert ! audioresample ! '
                        f'volume name=avol_{i} volume={vol} ! amix. '
                    )

        escaped = self._output_path.replace('"', '\\"')
        pipeline_str = (
            f"appsrc name=vsrc format=time is-live=true do-timestamp=true "
            f"caps=video/x-raw,format=BGR,width={w},height={h},framerate=30/1 ! "
            f"queue max-size-buffers=30 max-size-time=1000000000 leaky=downstream ! "
            f"videoconvert ! {enc_str} ! "
            f"{muxer} name=mux ! filesink location=\"{escaped}\" "
            f"{audio_str}"
        )

        log.info("Recording pipeline: %s", pipeline_str)
        log.info(
            "Audio sources: all=%s active=%s volumes=%s",
            self._audio_source_devices,
            list(self._active_audio_set),
            self._source_volumes,
        )
        try:
            self._pipeline = Gst.parse_launch(pipeline_str)
            self._vsrc = self._pipeline.get_by_name("vsrc")

            # Collect all pulsesrc elements for EOS on stop
            self._audio_srcs = []
            mic = self._pipeline.get_by_name("asrc_mic")
            if mic:
                self._audio_srcs.append(mic)
            for i in range(len(self._audio_source_devices)):
                el = self._pipeline.get_by_name(f"asrc_{i}")
                if el:
                    self._audio_srcs.append(el)

            # Collect volume elements for dynamic mute/unmute
            self._audio_vol_elements = {}
            mic_vol_el = self._pipeline.get_by_name("avol_mic")
            if mic_vol_el:
                self._audio_vol_elements["__mic__"] = mic_vol_el
                
            for i, dev in enumerate(self._audio_source_devices):
                vol_el = self._pipeline.get_by_name(f"avol_{i}")
                if vol_el:
                    self._audio_vol_elements[dev] = vol_el
                    log.info(
                        "avol_%d (%s): volume=%.1f",
                        i, dev, vol_el.get_property("volume"),
                    )

            bus = self._pipeline.get_bus()
            bus.add_signal_watch()
            bus.connect("message::error", self._on_error)

            ret = self._pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                log.error("Failed to start recording pipeline")
                self._stop_pipeline()
                return False
            return True
        except Exception as exc:
            log.error("Failed to create recording pipeline: %s", exc)
            return False

    def set_source_active(self, source_name: str, active: bool) -> None:
        """Mute or unmute a USB camera audio source in the recording pipeline."""
        if active:
            self._active_audio_set.add(source_name)
        else:
            self._active_audio_set.discard(source_name)
        vol_el = self._audio_vol_elements.get(source_name)
        if vol_el:
            if active and not self._global_muted:
                vol = self._source_volumes.get(source_name, 1.0)
            else:
                vol = 0.0
            vol_el.set_property("volume", vol)
            log.info("Recording audio %s: %s (vol=%.2f)", "unmuted" if active else "muted", source_name, vol)

    def set_muted(self, muted: bool) -> None:
        """Global mute/unmute all audio sources in the recording pipeline."""
        self._global_muted = muted
        for dev, vol_el in self._audio_vol_elements.items():
            if muted:
                vol_el.set_property("volume", 0.0)
            else:
                if dev == "__mic__":
                    vol_el.set_property("volume", 1.0)
                # Restore per-source volume if source is active
                elif dev in self._active_audio_set:
                    vol = self._source_volumes.get(dev, 1.0)
                    vol_el.set_property("volume", vol)
                # If not active, keep at 0.0
        log.info("Recording global mute: %s", muted)

    def set_source_volume(self, source_name: str, volume: float) -> None:
        """Update per-source volume in the recording pipeline."""
        volume = max(0.0, min(1.0, volume))
        self._source_volumes[source_name] = volume
        vol_el = self._audio_vol_elements.get(source_name)
        if vol_el:
            # Only apply if source is active and not globally muted
            if source_name in self._active_audio_set and not self._global_muted:
                vol_el.set_property("volume", volume)
                log.info("Recording source volume: %s = %.2f", source_name, volume)

    def write_frame(self, bgr: Any) -> None:
        """Push a processed BGR frame into the recording pipeline."""
        if not self._recording:
            return

        h, w = bgr.shape[:2]
        if not self._ensure_pipeline(w, h):
            return

        # Resize if camera changed resolution (e.g. camera switch while recording)
        if (w != self._w or h != self._h) and _HAS_CV2:
            bgr = cv2.resize(bgr, (self._w, self._h), interpolation=cv2.INTER_LINEAR)

        data = bgr.tobytes()
        buf = Gst.Buffer.new_wrapped(data)
        # We let appsrc (do-timestamp=true) handle the timestamps relative to pipeline start
        if self._vsrc:
            ret = self._vsrc.emit("push-buffer", buf)
            if ret != Gst.FlowReturn.OK:
                log.warning("Recording appsrc push error: %s", ret)

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
            # Capture references before clearing — finalization runs in background
            pipeline = self._pipeline
            vsrc = self._vsrc
            audio_srcs = list(self._audio_srcs)
            self._pipeline = None
            self._vsrc = None
            self._audio_srcs = []

            def _finalize():
                # Stop audio sources immediately and inject EOS downstream
                for asrc in audio_srcs:
                    src_pad = asrc.get_static_pad("src")
                    peer = src_pad.get_peer() if src_pad else None
                    asrc.set_state(Gst.State.NULL)
                    if peer:
                        peer.send_event(Gst.Event.new_eos())
                # Stop video source
                if vsrc:
                    vsrc.emit("end-of-stream")

                # Wait for EOS to propagate through mux → filesink
                bus = pipeline.get_bus()
                msg = bus.timed_pop_filtered(
                    10 * Gst.SECOND,
                    Gst.MessageType.EOS | Gst.MessageType.ERROR,
                )
                if msg and msg.type == Gst.MessageType.EOS:
                    log.info("Recording pipeline EOS received")
                elif msg and msg.type == Gst.MessageType.ERROR:
                    err, dbg = msg.parse_error()
                    log.error("Recording stop error: %s (%s)", err.message, dbg)
                else:
                    log.warning("Recording stop: EOS timeout after 10s")

                pipeline.set_state(Gst.State.NULL)
                self._remux_container(path)

            threading.Thread(target=_finalize, daemon=True).start()

        log.info("Recording stopped: %s", path)
        return path

    def _remux_container(self, path: str) -> None:
        """Remux container to fix metadata (duration, seek cues)."""
        if not os.path.isfile(path):
            return
        ext = self._container_ext()
        tmp = path + f".remux{ext}"
        try:
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", path, "-c", "copy", tmp],
                capture_output=True,
                timeout=120,
            )
            if result.returncode == 0 and os.path.isfile(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, path)
                log.info("Container metadata fixed: %s", os.path.basename(path))
            else:
                if os.path.isfile(tmp):
                    os.remove(tmp)
                log.warning("Remux failed: %s", result.stderr.decode(errors="replace")[:300])
        except FileNotFoundError:
            log.debug("ffmpeg not available for container remux")
        except subprocess.TimeoutExpired:
            log.warning("Remux timed out for %s", os.path.basename(path))
            if os.path.isfile(tmp):
                os.remove(tmp)

    def _stop_pipeline(self) -> None:
        if self._pipeline:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        self._vsrc = None
        self._audio_srcs = []
