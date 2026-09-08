"""Stream Engine – GStreamer pipeline lifecycle for camera preview."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from typing import Any

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gst, GstVideo, Gdk, GLib, GObject

import numpy as np

try:
    import cv2

    _HAS_CV2 = True
    # Suppress OpenCV WARN-level messages (e.g. V4L2 requestBuffers failures)
    os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
except ImportError:
    _HAS_CV2 = False

from constants import BackendType
from core.camera_backend import CameraInfo, VideoFormat
from core.camera_manager import CameraManager
from core.effects import EffectPipeline
from core.virtual_camera import VirtualCamera
from utils.i18n import _
from utils.async_worker import run_async
from utils.frame_buffers import bgr_from_bgra, LatestValue
from utils.settings_manager import SettingsManager
from utils.video_formats import frame_rate
from utils.urls import gst_quote
import time

Gst.init(None)
log = logging.getLogger(__name__)

# Backends that stream via UDP (MPEG-TS) need appsink
_APPSINK_BACKENDS = {BackendType.GPHOTO2, BackendType.IP}

# ── Thread-safe stderr suppression (refcounted) ─────────────────────
# Native libraries (libjpeg-turbo, V4L2) write warnings directly to fd 2.
# We redirect fd 2 to /dev/null while capture threads are active, using a
# refcount so multiple threads can coexist safely.
_stderr_lock = threading.Lock()
_stderr_refcount = 0
_stderr_orig_fd: int | None = None


def _stderr_suppress() -> None:
    # Kept for the legacy capture fallback. Never redirect process-wide fd 2.
    return None


def _stderr_restore() -> None:
    return None


def _find_device_users(device_path: str) -> list[str]:
    """Return list of process names currently using a V4L2 device.

    Filters out the current process (BigCam) so we only report *external*
    applications holding the device.
    """
    try:
        result = subprocess.run(
            ["fuser", device_path],
            capture_output=True,
            text=True,
            timeout=3,
        )
        pids = result.stdout.strip().split()
        own_pid = str(os.getpid())
        names: list[str] = []
        for pid in pids:
            pid = pid.strip().rstrip("m")
            if not pid.isdigit():
                continue
            if pid == own_pid:
                continue
            comm = f"/proc/{pid}/comm"
            if os.path.exists(comm):
                with open(comm) as f:
                    name = f.read().strip()
                    if name and name not in names:
                        names.append(name)
        return names
    except Exception:
        return []


class _BgVcamFeeder:
    """Background virtual camera feeder using OpenCV V4L2 capture.

    Reads frames from a physical camera via cv2.VideoCapture (V4L2 mmap)
    and pushes them to a v4l2loopback device via GStreamer appsrc -> v4l2sink.
    Runs in a daemon thread - safe to abandon on app exit.
    """

    def __init__(self, device_path: str, loopback_device: str, camera_name: str) -> None:
        self._device_path = device_path
        self._loopback = loopback_device
        self._name = camera_name
        self._stop = threading.Event()
        self._pipeline: Gst.Pipeline | None = None
        self._appsrc: Any = None
        self._thread: threading.Thread | None = None
        self._w = 0
        self._h = 0

    def start(self) -> bool:
        """Launch the feeder thread. Initialization happens in the background."""
        if not _HAS_CV2:
            return False
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=f"bgvcam-{self._name}",
        )
        self._thread.start()
        return True

    def _loop(self) -> None:
        """Live, bounded GStreamer forwarding; no concurrent OpenCV release/read."""
        pipeline = None
        try:
            source = f"v4l2src device={gst_quote(self._device_path)} ! decodebin"
            pipeline = Gst.parse_launch(
                f"{source} ! queue max-size-buffers=2 leaky=downstream ! videoconvert ! "
                "video/x-raw,format=YUY2 ! "
                f"v4l2sink device={gst_quote(self._loopback)} sync=false")
            self._pipeline = pipeline
            if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("Background camera could not start")
            bus = pipeline.get_bus()
            while not self._stop.wait(0.1):
                message = bus.timed_pop_filtered(0, Gst.MessageType.ERROR | Gst.MessageType.EOS)
                if message is not None:
                    if message.type == Gst.MessageType.ERROR:
                        log.warning("Background camera stopped after a stream error")
                    break
        except Exception:
            log.exception("Background camera forwarding failed")
        finally:
            if pipeline is not None:
                pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
            self._appsrc = None

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
            if thread.is_alive():
                log.warning("Background camera teardown is still pending")
            else:
                self._thread = None


class StreamEngine(GObject.Object):
    """Builds and manages the GStreamer preview pipeline for any camera backend."""

    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "error": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "device-busy": (GObject.SignalFlags.RUN_LAST, None, (str, object)),
        "new-texture": (GObject.SignalFlags.RUN_LAST, None, (object,)),
    }

    def __init__(self, camera_manager: CameraManager) -> None:
        super().__init__()
        self._manager = camera_manager
        self._settings = SettingsManager()
        self._generation = 0
        self._texture_slot = LatestValue()
        self._has_received_frame = False
        self._snapshot_request = threading.Event()
        self._snapshot_ready = threading.Event()
        self._vcam_resolving = False
        self._bg_pending = set()
        self._bg_generation = 0
        self._pipeline: Gst.Pipeline | None = None
        self._bus_watch_id: int | None = None
        self._current_camera: CameraInfo | None = None
        self._current_fmt: VideoFormat | None = None
        self._gtksink: Any = None
        self._use_appsink = False
        self._last_texture: Gdk.Texture | None = None
        self._frame_count: int = 0
        self._current_fps: float = 0.0
        self._fps_timer_id: int | None = None
        self._mirror: bool = False
        self._effects = EffectPipeline()
        self._probe_debug_count: int = 0
        self._probe_cached_fmt: str = ""
        self._probe_pad: Gst.Pad | None = None
        self._probe_id: int = 0
        self._last_probe_bgr = None
        self._overlay_rects: list[tuple] = []  # [(x,y,w,h), ...] for QR overlay
        self._qr_scan_active: bool = False  # whether QR scanning mode is on
        self._qr_scan_tick: int = 0  # animation counter for scanning guide
        self._video_recorder: Any = None  # set by window to enable phone recording
        self._zoom_level: float = 1.0  # 1.0 = no zoom, 2.0 = 2x zoom
        self._sharpness: float = 0.0  # 0.0 = off, positive = sharpen strength
        self._pan: float = 0.0   # -1.0 to 1.0 (left/right offset ratio)
        self._tilt: float = 0.0  # -1.0 to 1.0 (up/down offset ratio)
        # General virtual camera output (appsrc -> v4l2sink)
        self._vcam_pipeline: Gst.Pipeline | None = None
        self._vcam_appsrc: Any = None
        self._vcam_device: str = ""
        self._vcam_alloc_id: str = ""  # VirtualCamera allocation id for vcam device
        self._vcam_w: int = 0
        self._vcam_h: int = 0
        self._vcam_bgra_buf: np.ndarray | None = None
        self._prefer_v4l2: bool = True  # bypass PipeWire, use v4l2src directly
        # OpenCV direct capture (like guvcview) - used when prefer_v4l2 is active
        self._cv_cap: Any = None  # cv2.VideoCapture or None
        self._cv_timer_id: int | None = None  # GLib.timeout_add ID for frame poll
        # Flag: True while _rebuild_vcam is scheduled/running on the main thread.
        # Prevents multiple concurrent rebuild requests from the probe thread.
        self._vcam_building: bool = False
        # Most-recent frame queued while the vcam pipeline is being built.
        # Tuple of (bgra_bytes, w, h) or None.
        self._vcam_pending_frame: tuple | None = None
        # Deferred vcam setup: resolve after first frame is rendered on screen
        self._vcam_resolve_pending: bool = False
        # Latest frame for async vcam push (set by probe, consumed by idle)
        self._vcam_latest_frame: tuple | None = None
        self._vcam_idle_scheduled: bool = False
        # Background virtual camera pipelines (camera_id -> pipeline)
        self._bg_vcam_pipelines: dict[str, Gst.Pipeline] = {}
        # Background OpenCV-based vcam feeders (camera_id -> _BgVcamFeeder)
        self._bg_vcam_feeders: dict[str, _BgVcamFeeder] = {}
        # USB autosuspend: saved original power/control value
        self._usb_power_control_path: str = ""
        self._usb_power_control_orig: str = ""


    @property
    def effects(self) -> EffectPipeline:
        return self._effects

    @property
    def last_frame_bgr(self):
        """Return the last BGR frame (numpy array) from the probe, or None."""
        return self._last_probe_bgr

    def set_overlay_rects(self, rects: list[tuple]) -> None:
        """Set rectangles to draw on the video feed (e.g. QR bounding boxes)."""
        self._overlay_rects = rects

    def set_qr_scanning(self, active: bool) -> None:
        """Enable/disable QR scanning guide overlay."""
        self._qr_scan_active = active
        self._qr_scan_tick = 0

    def set_zoom(self, level: float) -> None:
        """Set digital zoom level (1.0 = no zoom, up to 4.0)."""
        self._zoom_level = max(1.0, min(4.0, level))
        self._update_crop()

    def set_sharpness(self, level: float) -> None:
        """Set hardware sharpness (0.0 = off, up to 1.0 = max)."""
        self._sharpness = max(0.0, min(1.0, level))
        self._update_sharpness()

    def set_pan(self, value: float) -> None:
        """Set pan offset (-1.0 left .. 1.0 right)."""
        self._pan = max(-1.0, min(1.0, value))
        self._update_crop()

    def set_tilt(self, value: float) -> None:
        """Set tilt offset (-1.0 up .. 1.0 down)."""
        self._tilt = max(-1.0, min(1.0, value))
        self._update_crop()

    def _update_crop(self) -> None:
        if not self._pipeline:
            return
        crop = self._pipeline.get_by_name("crop")
        if not crop:
            return
            
        pad = crop.get_static_pad("sink")
        if not pad:
            return
        caps = pad.get_current_caps()
        if not caps:
            return
        s = caps.get_structure(0)
        zw = s.get_value("width")
        zh = s.get_value("height")
        if not zw or not zh:
            return

        zoom = self._zoom_level
        if (self._pan != 0.0 or self._tilt != 0.0) and zoom < 1.5:
            zoom = 1.5

        crop_h = int(zh / zoom)
        crop_w = int(zw / zoom)
        cx = zw // 2 + int(self._pan * (zw - crop_w) / 2)
        cy = zh // 2 + int(self._tilt * (zh - crop_h) / 2)
        
        left = max(0, min(cx - crop_w // 2, zw - crop_w))
        top = max(0, min(cy - crop_h // 2, zh - crop_h))
        right = zw - (left + crop_w)
        bottom = zh - (top + crop_h)

        crop.set_property("left", left)
        crop.set_property("right", right)
        crop.set_property("top", top)
        crop.set_property("bottom", bottom)

    def _update_sharpness(self) -> None:
        if not self._pipeline:
            return
        sharp = self._pipeline.get_by_name("sharp")
        if not sharp:
            return
        amount = self._sharpness * 2.0
        sharp.set_property("amount", amount)

    # -- public API ----------------------------------------------------------

    @property
    def current_camera(self) -> CameraInfo | None:
        return self._current_camera

    @property
    def paintable(self) -> Any | None:
        """Return the GdkPaintable for embedding in GtkPicture (gtk4paintablesink only)."""
        if self._gtksink and not self._use_appsink:
            return self._gtksink.get_property("paintable")
        return None

    @property
    def uses_appsink(self) -> bool:
        return self._use_appsink

    @property
    def pipeline(self) -> Gst.Pipeline | None:
        return self._pipeline

    @property
    def fps(self) -> float:
        return self._current_fps

    def _start_fps_counter(self) -> None:
        self._frame_count = 0
        self._fps_last_time = time.monotonic()
        self._current_fps = 0.0
        if self._fps_timer_id is not None:
            GLib.source_remove(self._fps_timer_id)
        self._fps_timer_id = GLib.timeout_add(1000, self._update_fps_counter)

    def _stop_fps_counter(self) -> None:
        if self._fps_timer_id is not None:
            GLib.source_remove(self._fps_timer_id)
            self._fps_timer_id = None
        self._current_fps = 0.0

    def _update_fps_counter(self) -> bool:
        now = time.monotonic()
        self._current_fps = self._frame_count / max(now - self._fps_last_time, 0.001)
        self._fps_last_time = now
        self._frame_count = 0
        return GLib.SOURCE_CONTINUE

    def _on_frame_probe(
        self, pad: Gst.Pad, info: Gst.PadProbeInfo
    ) -> Gst.PadProbeReturn:
        self._frame_count += 1
        return Gst.PadProbeReturn.OK

    # -- shared frame processing ---------------------------------------------

    def _apply_frame_processing(self, bgr: np.ndarray) -> np.ndarray:
        """Apply software effects to a BGR frame (effects, QR overlay).
        Note: Zoom and Sharpness are now handled natively via GPU in GStreamer."""
        if self._pipeline is None and (self._zoom_level > 1 or self._pan or self._tilt):
            h, w = bgr.shape[:2]
            zoom = max(self._zoom_level, 1.5 if self._pan or self._tilt else 1.0)
            cw, ch = max(1, int(w / zoom)), max(1, int(h / zoom))
            x = int((w - cw) * (self._pan + 1) / 2)
            y = int((h - ch) * (self._tilt + 1) / 2)
            bgr = cv2.resize(bgr[y:y + ch, x:x + cw], (w, h))
        if self._effects.has_active_effects():
            bgr = self._effects.apply(bgr)

        # Fallback to software sharpness if hardware plugin unsharp is not available
        if getattr(self, '_sharpness', 0.0) > 0.0 and self._pipeline and not self._pipeline.get_by_name("sharp"):
            amount = self._sharpness * 2.0
            blurred = cv2.GaussianBlur(bgr, (0, 0), 3)
            bgr = cv2.addWeighted(bgr, 1.0 + amount, blurred, -amount, 0)

        # QR detection overlay
        if self._overlay_rects:
            # Dim entire frame efficiently, then restore detected regions
            overlay = cv2.convertScaleAbs(bgr, alpha=0.4)
            for rect in self._overlay_rects:
                x, y, rw, rh = rect
                overlay[y:y + rh, x:x + rw] = bgr[y:y + rh, x:x + rw]
                cv2.rectangle(overlay, (x, y), (x + rw, y + rh), (0, 255, 0), 3)
            bgr = overlay
        elif self._qr_scan_active:
            fh, fw = bgr.shape[:2]
            side = min(fw, fh) * 2 // 3
            cx, cy = fw // 2, fh // 2
            x1, y1 = cx - side // 2, cy - side // 2
            x2, y2 = x1 + side, y1 + side
            corner_len = side // 5
            self._qr_scan_tick += 1
            # Dim frame, restore scan window
            overlay = cv2.convertScaleAbs(bgr, alpha=0.4)
            overlay[y1:y2, x1:x2] = bgr[y1:y2, x1:x2]
            scan_range = y2 - y1
            scan_pos = y1 + int((self._qr_scan_tick % 60) / 60.0 * scan_range)
            cv2.line(overlay, (x1 + 4, scan_pos), (x2 - 4, scan_pos), (0, 200, 255), 2)
            color = (255, 255, 255)
            t = 3
            cv2.line(overlay, (x1, y1), (x1 + corner_len, y1), color, t)
            cv2.line(overlay, (x1, y1), (x1, y1 + corner_len), color, t)
            cv2.line(overlay, (x2, y1), (x2 - corner_len, y1), color, t)
            cv2.line(overlay, (x2, y1), (x2, y1 + corner_len), color, t)
            cv2.line(overlay, (x1, y2), (x1 + corner_len, y2), color, t)
            cv2.line(overlay, (x1, y2), (x1, y2 - corner_len), color, t)
            cv2.line(overlay, (x2, y2), (x2 - corner_len, y2), color, t)
            cv2.line(overlay, (x2, y2), (x2, y2 - corner_len), color, t)
            bgr = overlay
        return bgr

    def _distribute_processed_frame(
        self, bgr: np.ndarray, w: int, h: int,
        bgra_direct: bytes | None = None,
    ) -> None:
        """Store processed frame and feed to vcam/recorder.

        When *bgra_direct* is provided (fast path), push it straight to the
        virtual camera without an extra BGR->BGRA conversion.
        """
        self._last_probe_bgr = bgr
        if self._snapshot_request.is_set():
            self._snapshot_ready.set()
        if self._vcam_device and self._last_probe_bgr is not None:
            if bgra_direct is not None:
                self._schedule_vcam_push(bgra_direct, w, h)
            else:
                if self._vcam_bgra_buf is None or self._vcam_bgra_buf.shape[:2] != (h, w):
                    self._vcam_bgra_buf = np.empty((h, w, 4), dtype=np.uint8)
                cv2.cvtColor(self._last_probe_bgr, cv2.COLOR_BGR2BGRA, dst=self._vcam_bgra_buf)
                self._schedule_vcam_push(self._vcam_bgra_buf.tobytes(), w, h)
        if self._video_recorder and self._video_recorder.is_recording:
            self._video_recorder.write_frame(self._last_probe_bgr)

    def _has_processing_work(self) -> bool:
        """Check if any frame processing is needed."""
        return (self._effects.has_active_effects() or self._overlay_rects
                or self._qr_scan_active
                or self._zoom_level > 1.0 or self._sharpness > 0.0
                or self._pan != 0.0 or self._tilt != 0.0)

    def _on_paintable_probe(self, pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        generation = self._generation
        self._frame_count += 1
        self._notify_first_frame(generation)
        work = self._has_processing_work()
        recording = self._video_recorder and self._video_recorder.is_recording
        if not work and not recording and not self._vcam_device and not self._snapshot_request.is_set() and self._frame_count % 10:
            return Gst.PadProbeReturn.OK
        buf, caps = info.get_buffer(), pad.get_current_caps()
        if buf is None or caps is None:
            return Gst.PadProbeReturn.OK
        try:
            bgr = self._read_bgra_buffer(buf, caps)
            h, w = bgr.shape[:2]
            processed = self._apply_frame_processing(bgr) if work else bgr
            if generation != self._generation:
                return Gst.PadProbeReturn.OK
            self._distribute_processed_frame(processed, w, h)
            if work:
                # Only used on runtimes that expose PadProbeInfo.set_buffer.
                # A packed output has a new VideoMeta consistent with its actual stride.
                output = Gst.Buffer.new_wrapped(cv2.cvtColor(processed, cv2.COLOR_BGR2BGRA).tobytes())
                output.pts, output.dts, output.duration = buf.pts, buf.dts, buf.duration
                output.offset, output.offset_end = buf.offset, buf.offset_end
                GstVideo.buffer_add_video_meta(output, GstVideo.VideoFrameFlags.NONE,
                                              GstVideo.VideoFormat.BGRA, w, h)
                info.set_buffer(output)
        except Exception:
            log.exception("Could not process a video buffer")
        return Gst.PadProbeReturn.OK

    @property
    def mirror(self) -> bool:
        return self._mirror

    @mirror.setter
    def mirror(self, value: bool) -> None:
        self._mirror = value

    @property
    def prefer_v4l2(self) -> bool:
        return self._prefer_v4l2

    @prefer_v4l2.setter
    def prefer_v4l2(self, value: bool) -> None:
        self._prefer_v4l2 = value

    def capture_snapshot(self, output_path: str) -> bool:
        """Save a fresh processed frame. Call from a worker, not the GTK main loop."""
        generation = self._generation
        self._snapshot_ready.clear()
        self._snapshot_request.set()
        try:
            if not self._snapshot_ready.wait(timeout=1.0):
                return False
            frame = self._last_probe_bgr
            if generation != self._generation or frame is None:
                return False
            return bool(cv2.imwrite(output_path, frame.copy()))
        except (OSError, cv2.error):
            log.exception("Could not save the captured frame")
            return False
        finally:
            self._snapshot_request.clear()

    def play(
        self,
        camera: CameraInfo,
        fmt: VideoFormat | None = None,
        streaming_ready: bool = False,
    ) -> bool:
        """Build and start the pipeline for *camera*.

        Args:
            streaming_ready: If True, skip start_streaming() because caller
                             already handled it (e.g. window async setup).
        """
        # Avoid tearing down a running pipeline for the same camera+format
        if (
            self._current_camera
            and self._current_camera.id == camera.id
            and self.is_playing()
            and fmt == self._current_fmt
        ):
            log.debug("play(): camera %s already playing, skipping restart", camera.name)
            return True

        log.info("play() called: camera=%s id=%s, bg_vcams=%s, bg_feeders=%s",
                 camera.name, camera.id,
                 list(self._bg_vcam_pipelines.keys()),
                 list(self._bg_vcam_feeders.keys()))
        self.stop(stop_backend=False, keep_vcam=True)
        self._current_camera = camera
        self._current_fmt = fmt
        self._play_busy_retries = 0
        # If this camera had a background vcam (GStreamer or OpenCV feeder),
        # stop it - we'll create a new effects-aware one. The bg source holds
        # an exclusive lock on the device; after stopping, allow the kernel a
        # moment to release it before opening again.
        had_bg_vcam = (
            camera.id in self._bg_vcam_pipelines
            or camera.id in self._bg_vcam_feeders
        )
        self._stop_bg_vcam(camera.id)
        if had_bg_vcam:
            # Device needs a moment to be fully released - defer the rest.
            # 100ms is usually enough for the kernel to release the V4L2 handle.
            GLib.timeout_add(100, self._play_continue, camera, fmt, streaming_ready)
            return True
        return self._play_continue(camera, fmt, streaming_ready)

    def _play_continue(self, camera: CameraInfo, fmt: VideoFormat | None, streaming_ready: bool) -> bool:
        """Continuation of play() - may be deferred via GLib.timeout_add.
        Always returns False so GLib.timeout_add won't repeat."""
        if self._current_camera is not camera:
            return GLib.SOURCE_REMOVE
        self._use_appsink = camera.backend in _APPSINK_BACKENDS
        log.info(
            "play: camera=%s, backend=%s, use_appsink=%s, streaming_ready=%s",
            camera.name, camera.backend, self._use_appsink, streaming_ready,
        )

        # Phone camera – frames come via WebRTC, no GStreamer pipeline needed
        if camera.backend == BackendType.PHONE:
            return self._start_phone_camera(camera)

        # Some backends need an external streaming process first
        if not streaming_ready:
            backend = self._manager.get_backend(camera.backend)
            if (
                backend
                and hasattr(backend, "needs_streaming_setup")
                and backend.needs_streaming_setup()
            ):
                if not backend.start_streaming(camera):
                    self.emit("error", _("Failed to start camera streaming process."))
                    return False

        # Resolve GStreamer source in background (pw-dump can take seconds)
        generation = self._generation
        def _resolve_source() -> str:
            return self._manager.get_gst_source(
                camera, fmt, prefer_v4l2=self._prefer_v4l2,
            ) or ""

        def _on_source_resolved(gst_source: str) -> None:
            # Guard: camera may have changed while resolving
            if self._current_camera is not camera or generation != self._generation:
                return
            if not gst_source:
                self.emit("error", _("Failed to obtain GStreamer source for this camera."))
                return

            target_fps = 0
            if fmt and fmt.fps:
                target_fps = max(fmt.fps)

            if self._use_appsink:
                self._build_appsink_pipeline(gst_source)
            else:
                self._build_paintable_pipeline(gst_source, target_fps)

        threading.Thread(
            target=lambda: GLib.idle_add(_on_source_resolved, _resolve_source()),
            daemon=True,
        ).start()
        return False

    def _build_paintable_pipeline(self, gst_source: str, target_fps: int = 0) -> bool:
        """Direct camera sources - use tee + gtk4paintablesink (recording-ready).

        Virtual camera output uses a separate appsrc -> v4l2sink pipeline fed
        from the probe callback, ensuring OpenCV effects are applied.

        When prefer_v4l2 is active, use v4l2src (bypassing PipeWire) for
        lower-latency access, but still render via gtk4paintablesink for
        GPU-accelerated display.
        """
        # NOTE: The old OpenCV direct capture (_build_direct_pipeline) is kept
        # as fallback but no longer attempted first - gtk4paintablesink with
        # v4l2src provides smoother rendering via GPU texture uploads rather
        # than CPU-side GdkMemoryTexture copies (~25 MB/frame).
        if not hasattr(Gst.PadProbeInfo, "set_buffer") or not Gst.ElementFactory.find("gtk4paintablesink"):
            log.info("Using the compatible appsink renderer (no pad-buffer replacement API)")
            return self._build_appsink_pipeline(gst_source)

        is_phone = self._current_camera and self._current_camera.id.startswith("phone:")

        n_threads = min(os.cpu_count() or 2, 4)
        suffix = (
            f"videoflip name=flip method=0 ! "
            f"videocrop name=crop left=0 right=0 top=0 bottom=0 ! "
            f"videoconvert n-threads={n_threads} name=conv ! "
            f"video/x-raw,format=BGRA ! "
            f"tee name=t ! "
            f"queue max-size-buffers=4 leaky=downstream silent=true ! "
            f"gtk4paintablesink sync=true max-lateness=-1 qos=false"
        )

        base_pipeline = f"{gst_source} ! {suffix}"

        if self._try_start_paintable(base_pipeline):
            # Anti-flicker disabled: calling v4l2-ctl on cheap USB cameras
            # while the pipeline is running disrupts UVC stream stability.
            # self._apply_anti_flicker_async()
            # Disable USB autosuspend to prevent frame drops
            if self._current_camera and self._current_camera.device_path:
                self._disable_usb_autosuspend(self._current_camera.device_path)
            # Defer vcam setup until the first frame renders on screen
            self._vcam_resolve_pending = True
            return True

        # PipeWire source may fail on some format/fps combinations.
        # Fallback: try V4L2 direct access if the original source was PipeWire.
        camera = self._current_camera
        if camera and "pipewiresrc" in gst_source and camera.device_path:
            log.warning(
                "PipeWire pipeline failed for %s, falling back to v4l2src",
                camera.device_path,
            )
            backend = self._manager.get_backend(camera.backend)
            if backend and hasattr(backend, "_v4l2_gst_source"):
                fmt_obj = None
                if camera.formats:
                    fmt_obj = backend._pick_best_format(camera)
                v4l2_source = backend._v4l2_gst_source(camera.device_path, camera, fmt_obj)
                fallback_pipeline = f"{v4l2_source} ! {suffix}"
                if self._try_start_paintable(fallback_pipeline):
                    # self._apply_anti_flicker_async()
                    if camera.device_path:
                        self._disable_usb_autosuspend(camera.device_path)
                    self._vcam_resolve_pending = True
                    return True

        # All pipelines failed - check if device is busy (in background)
        if camera and camera.device_path:
            self._check_device_busy_async(camera.device_path)
            return False

        self.emit("error", _("Failed to start camera stream."))
        return False

    def _try_start_paintable(self, pipeline_str: str) -> bool:
        """Try to parse and start a paintable pipeline. Returns True on success."""
        log.debug("Building paintable pipeline")
        try:
            pipeline = Gst.parse_launch(pipeline_str)
        except GLib.Error as exc:
            log.warning("Pipeline parse error: %s", exc)
            return False

        if not isinstance(pipeline, Gst.Pipeline):
            pipe = Gst.Pipeline.new("bigcam")
            pipe.add(pipeline)
            pipeline = pipe

        gtksink = None
        it = pipeline.iterate_sinks()
        while True:
            ret, elem = it.next()
            if ret == Gst.IteratorResult.OK:
                factory = elem.get_factory()
                if factory and factory.get_name() == "gtk4paintablesink":
                    gtksink = elem
                    break
            else:
                break

        if gtksink is None:
            pipeline.set_state(Gst.State.NULL)
            return False

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus_watch_id = bus.connect("message", self._on_bus_message)

        ret = pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            bus.disconnect(bus_watch_id)
            bus.remove_signal_watch()
            pipeline.set_state(Gst.State.NULL)
            return False

        self._pipeline = pipeline
        self._gtksink = gtksink
        self._bus_watch_id = bus_watch_id
        # Install effects/FPS probe on the tee's sink pad so effects
        # are applied to BOTH preview and virtual camera output.
        tee = pipeline.get_by_name("t")
        probe_pad = tee.get_static_pad("sink") if tee else None
        if not probe_pad:
            # Fallback: probe on the gtk4paintablesink (effects won't reach v4l2)
            probe_pad = gtksink.get_static_pad("sink")
        if probe_pad:
            self._probe_id = probe_pad.add_probe(Gst.PadProbeType.BUFFER, self._on_paintable_probe)
            self._probe_pad = probe_pad
        self._start_fps_counter()
        # Playing is announced only after a real video frame arrives.

        return True

    def _build_direct_pipeline(self, gst_source: str, target_fps: int = 0) -> bool:
        """OpenCV V4L2 direct capture - flicker-free like guvcview.

        Bypasses GStreamer entirely. A background thread captures frames via
        cv2.VideoCapture (V4L2 mmap + libjpeg-turbo), stores the latest in
        _cv_latest_frame. A GLib timer on the main thread picks up new frames
        and renders them as GdkMemoryTexture - never blocks the UI.
        """
        camera = self._current_camera
        if not camera or not camera.device_path:
            return False

        cap = cv2.VideoCapture(camera.device_path, cv2.CAP_V4L2)
        if not cap.isOpened():
            log.warning("OpenCV V4L2 failed to open %s", camera.device_path)
            cap.release()
            return False

        # Configure format to match what BigCam normally uses
        # For phone cameras (v4l2loopback), skip MJPG - the writer sets the
        # raw format (YUY2/NV12) and forcing MJPG can cause SIGBUS/SIGSEGV.
        is_phone = camera.id.startswith("phone:")
        if not is_phone:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc('M', 'J', 'P', 'G'))
        fmt = self._current_fmt
        if fmt:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, fmt.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, fmt.height)
            if fmt.fps:
                cap.set(cv2.CAP_PROP_FPS, max(fmt.fps))
        elif not is_phone:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            cap.set(cv2.CAP_PROP_FPS, 30)

        # Read one test frame (blocking, but only at startup)
        ret, test_frame = cap.read()
        if not ret:
            log.warning("OpenCV V4L2 failed to read test frame from %s", camera.device_path)
            cap.release()
            return False

        log.info(
            "OpenCV V4L2 capture started: %s %dx%d@%.0ffps (backend=%s)",
            camera.device_path,
            int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            cap.get(cv2.CAP_PROP_FPS),
            cap.getBackendName(),
        )

        self._cv_cap = cap
        self._cv_latest_frame: np.ndarray | None = test_frame
        self._cv_frame_seq: int = 0
        self._cv_rendered_seq: int = -1
        self._cv_stop_event = threading.Event()
        self._use_appsink = True
        self._gtksink = None
        self._pipeline = None

        # Background capture thread - reads frames as fast as the camera
        # delivers them (V4L2 mmap blocking read) and stores the latest.
        self._cv_thread = threading.Thread(
            target=self._cv_capture_loop, daemon=True, name="bigcam-cv-capture"
        )
        self._cv_thread.start()

        # Main-thread timer picks up new frames and renders them (non-blocking).
        interval = 33 if target_fps <= 0 else max(16, 1000 // target_fps)
        self._cv_timer_id = GLib.timeout_add(interval, self._cv_render_frame)

        self._start_fps_counter()
        # self._apply_anti_flicker_async()
        if camera.device_path:
            self._disable_usb_autosuspend(camera.device_path)
        self._vcam_resolve_pending = True
        # Playing is announced only after a real video frame arrives.
        log.info("Direct OpenCV V4L2 preview started (no GStreamer)")
        return True

    def _cv_capture_loop(self) -> None:
        """Background thread: read frames from V4L2 as fast as they arrive."""
        cap = self._cv_cap
        stop = self._cv_stop_event
        _stderr_suppress()
        try:
            while cap is not None and cap.isOpened() and not stop.is_set():
                ret, frame = cap.read()
                if ret:
                    self._cv_latest_frame = frame
                    self._cv_frame_seq += 1
                elif stop.is_set():
                    break
        finally:
            _stderr_restore()

    def _cv_render_frame(self) -> bool:
        """Main-thread timer: render latest captured frame as GdkTexture."""
        if self._cv_cap is None:
            return False  # stop timer

        # Check for new frame
        if self._cv_rendered_seq >= self._cv_frame_seq:
            return True  # no new frame yet, keep timer alive
        frame = self._cv_latest_frame
        if frame is None:
            return True

        self._cv_rendered_seq = self._cv_frame_seq
        self._frame_count += 1

        # Deferred vcam: resolve after first frame is rendered
        if self._vcam_resolve_pending:
            self._vcam_resolve_pending = False
            self._resolve_vcam_async()

        h, w = frame.shape[:2]
        bgr = frame.copy()  # copy to avoid race with capture thread

        bgr = self._apply_frame_processing(bgr)
        self._distribute_processed_frame(bgr, w, h)

        # Convert to BGRA for GdkTexture rendering
        # Mirror is handled by MirroredPicture in the GTK layer
        bgra = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
        data = bgra.tobytes()
        stride = w * 4
        glib_bytes = GLib.Bytes.new(data)
        self._update_texture(w, h, stride, glib_bytes)
        return True  # continue timer

    def _build_appsink_pipeline(self, gst_source: str) -> bool:
        self._use_appsink = True
        self._appsink_source = gst_source
        self._appsink_retry_count = 0
        self._appsink_max_retries = 3
        self._appsink_timer_id = GLib.timeout_add(100, self._try_appsink_first)
        # Virtual-camera creation is deferred until an actual frame arrives.
        return True

    def _ensure_vcam_with_retry(self, cam_id: str, cam_name: str | None) -> bool:
        if self._current_camera and self._current_camera.id == cam_id:
            self._resolve_vcam_async()
        return GLib.SOURCE_REMOVE

    def _try_appsink_first(self) -> bool:
        """First attempt after initial delay, then switch to 500ms retries."""
        log.debug("_try_appsink_first called")
        self._appsink_timer_id = None
        if self._try_appsink_pipeline():
            # Need to retry - schedule at 500ms intervals
            log.debug("First attempt failed, scheduling 500ms retries")
            self._appsink_timer_id = GLib.timeout_add(500, self._try_appsink_pipeline)
        else:
            log.debug("First attempt: done (success or gave up)")
        return False  # don't repeat the 2s timer

    def _try_appsink_pipeline(self) -> bool:
        if self._current_camera is None:
            self._appsink_timer_id = None
            return GLib.SOURCE_REMOVE
        self._appsink_retry_count += 1
        generation = self._generation
        pipeline = None
        try:
            pipeline = Gst.parse_launch(
                f"{self._appsink_source} ! videoflip name=flip method=0 ! "
                "videocrop name=crop left=0 right=0 top=0 bottom=0 ! "
                "videoconvert ! video/x-raw,format=BGRA ! "
                "queue max-size-buffers=2 leaky=downstream ! "
                "appsink name=sink emit-signals=true drop=true max-buffers=2 sync=false")
            sink = pipeline.get_by_name("sink")
            sink.connect("new-sample", self._on_appsink_sample, generation)
            bus = pipeline.get_bus()
            bus.add_signal_watch()
            watch = bus.connect("message", self._on_bus_message)
            self._pipeline, self._bus_watch_id = pipeline, watch
            if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("The camera pipeline could not start")
            self._start_fps_counter()
            self._appsink_timer_id = None
            return GLib.SOURCE_REMOVE
        except Exception:
            log.warning("Camera pipeline setup failed (attempt %s)", self._appsink_retry_count, exc_info=True)
            if pipeline is not None:
                bus = pipeline.get_bus()
                if self._bus_watch_id is not None:
                    bus.disconnect(self._bus_watch_id)
                    bus.remove_signal_watch()
                    self._bus_watch_id = None
                pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
        if self._appsink_retry_count < self._appsink_max_retries:
            return GLib.SOURCE_CONTINUE
        self._appsink_timer_id = None
        self.emit("error", _("Failed to start camera stream."))
        return GLib.SOURCE_REMOVE

    def _start_pipeline(self) -> bool:
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_watch_id = bus.connect("message", self._on_bus_message)

        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self.emit("error", _("Failed to start camera stream."))
            self.stop()
            return False

        # Playing is announced only after a real video frame arrives.
        return True

    def stop(self, stop_backend: bool = True, keep_vcam: bool = False) -> None:
        self._generation += 1
        self._texture_slot.clear()
        self._has_received_frame = False
        self._vcam_resolving = False
        self._snapshot_ready.set()
        camera = self._current_camera
        self._stop_fps_counter()
        self._restore_usb_autosuspend()

        # Stop OpenCV direct capture if active
        if hasattr(self, '_cv_stop_event') and self._cv_stop_event is not None:
            self._cv_stop_event.set()
        if self._cv_timer_id is not None:
            GLib.source_remove(self._cv_timer_id)
            self._cv_timer_id = None
        if hasattr(self, '_cv_thread') and self._cv_thread is not None:
            self._cv_thread.join(timeout=2.0)
            self._cv_thread = None
        if self._cv_cap is not None:
            self._cv_cap.release()
            self._cv_cap = None
        if hasattr(self, '_cv_stop_event'):
            self._cv_stop_event = None

        # Cancel any pending appsink retry timer
        if hasattr(self, "_appsink_timer_id") and self._appsink_timer_id is not None:
            GLib.source_remove(self._appsink_timer_id)
            self._appsink_timer_id = None

        # Phone camera: keep forwarding frames to vcam when keep_vcam is active,
        # otherwise disconnect completely.
        if self._phone_server_ref is not None:
            if keep_vcam and camera and self._vcam_device:
                # Detach from preview rendering but keep vcam v4l2 output alive.
                # Switch callback to background-only mode (no texture updates).
                self._phone_server_ref.set_frame_callback(self._on_phone_frame_bg)
                self._bg_phone_server_ref = self._phone_server_ref
                self._bg_phone_cam_id = camera.id
            else:
                self._phone_server_ref.set_frame_callback(None)
                self._stop_phone_v4l2()
                self._phone_v4l2_device = ""
            self._phone_server_ref = None
            self._phone_frame_pending = False
            self._current_camera = None
            self._current_fmt = None
            self.emit("state-changed", "stopped")

        # Release retained frame data to free memory
        self._last_probe_bgr = None
        self._last_texture = None
        self._vcam_latest_frame = None
        self._vcam_pending_frame = None
        self._vcam_resolve_pending = False
        self._vcam_bgra_buf = None
        self._probe_cached_fmt = ""
        # Remove buffer probe before pipeline teardown
        if self._probe_pad is not None and self._probe_id:
            self._probe_pad.remove_probe(self._probe_id)
            self._probe_pad = None
            self._probe_id = 0

        # Release effect caches to free memory
        from core.effects import release_segmenter
        release_segmenter()

        # Stop main GStreamer pipeline FIRST - releases the device/UDP port
        # so that background vcam pipelines can bind to them.
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            bus = self._pipeline.get_bus()
            if bus and self._bus_watch_id is not None:
                bus.disconnect(self._bus_watch_id)
                bus.remove_signal_watch()
                self._bus_watch_id = None
            self._pipeline = None
            self._gtksink = None
            self._current_camera = None
            self._current_fmt = None
            self.emit("state-changed", "stopped")

        # Virtual camera: keep alive via background pipeline or stop completely.
        # Done AFTER main pipeline is stopped so UDP port / device is free.
        if keep_vcam and camera:
            vcam_dev = self._vcam_device or VirtualCamera.get_device_for_camera(camera.id)
            # Don't promote to background if vcam device == camera source
            # (phone cameras already stream to a v4l2loopback).
            if vcam_dev and vcam_dev != camera.device_path:
                self._vcam_device = vcam_dev
                self._promote_vcam_to_background(camera)
            else:
                self._stop_vcam()
                self._release_vcam_device()
                self._vcam_device = ""
        else:
            self._stop_vcam()
            self._release_vcam_device()
            self._vcam_device = ""

        if camera and stop_backend:
            backend = self._manager.get_backend(camera.backend)
            if backend and hasattr(backend, "stop_streaming"):
                backend.stop_streaming(camera)

    def is_playing(self) -> bool:
        return self._current_camera is not None and self._has_received_frame

    # -- appsink rendering ---------------------------------------------------

    _appsink_sample_count = 0

    def _on_appsink_sample(self, appsink: Any, generation: int | None = None) -> Gst.FlowReturn:
        if generation is None:
            generation = self._generation
        sample = appsink.emit("pull-sample")
        if sample is None or generation != self._generation or self._current_camera is None:
            return Gst.FlowReturn.OK
        try:
            bgr = self._read_bgra_buffer(sample.get_buffer(), sample.get_caps())
            bgr = self._apply_frame_processing(bgr)
            if generation != self._generation:
                return Gst.FlowReturn.OK
            h, w = bgr.shape[:2]
            self._frame_count += 1
            self._distribute_processed_frame(bgr, w, h)
            self._notify_first_frame(generation)
            self._queue_texture(bgr, generation)
        except Exception:
            log.exception("Could not process appsink frame")
        return Gst.FlowReturn.OK

    def _update_texture(
        self, w: int, h: int, stride: int, glib_bytes: GLib.Bytes
    ) -> bool:
        # Discard stale appsink frames if pipeline mode changed to paintable
        if not self._use_appsink:
            return False
        try:
            texture = Gdk.MemoryTexture.new(
                w, h, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED, glib_bytes, stride
            )
            self._last_texture = texture
            self.emit("new-texture", texture)
        except Exception:
            pass
        return False

    # -- async helpers for non-blocking pipeline setup -----------------------

    def _apply_anti_flicker_async(self) -> None:
        """Apply anti-flicker V4L2 defaults in a background thread."""
        camera = self._current_camera
        if not camera or not camera.device_path:
            return
        threading.Thread(
            target=self._manager.apply_anti_flicker,
            args=(camera,),
            daemon=True,
        ).start()

    def _disable_usb_autosuspend(self, device_path: str) -> None:
        # Power management belongs to the administrator; BigCam does not alter sysfs.
        return None

    def _restore_usb_autosuspend(self) -> None:
        """Restore USB autosuspend to original value after stopping."""
        if not self._usb_power_control_path or not self._usb_power_control_orig:
            return
        try:
            with open(self._usb_power_control_path, "w") as f:
                f.write(self._usb_power_control_orig)
            log.info("USB autosuspend restored: %s -> %s",
                     self._usb_power_control_path, self._usb_power_control_orig)
        except OSError as exc:
            log.debug("Cannot restore USB power control: %s", exc)
        self._usb_power_control_path = ""
        self._usb_power_control_orig = ""

    def _resolve_vcam_async(self) -> None:
        camera = self._current_camera
        if not camera or self._vcam_resolving or self._vcam_device or not VirtualCamera.is_enabled():
            return
        if camera.id in self._settings.get("vcam-disabled-cameras", []):
            return
        generation = self._generation
        alloc_id = camera.id
        self._vcam_resolving = True
        def worker():
            return VirtualCamera.ensure_ready(camera_id=alloc_id, card_label=camera.name)
        def done(device):
            if generation == self._generation:
                self._vcam_resolving = False
            if not device:
                return
            if generation != self._generation or self._current_camera is not camera or not VirtualCamera.is_enabled():
                VirtualCamera.release_device(alloc_id)
                return
            if device == camera.device_path:
                VirtualCamera.release_device(alloc_id)
                log.error("Refusing virtual-camera feedback loop")
                return
            self._vcam_alloc_id = alloc_id
            self._start_vcam(device)
        def failed(error):
            if generation == self._generation:
                self._vcam_resolving = False
            log.warning("Virtual-camera setup failed: %s", error)
        run_async(worker, on_success=done, on_error=failed)

    def _check_device_busy_async(self, device_path: str) -> None:
        """Check if a device is busy in a background thread."""
        def _worker() -> list[str]:
            return _find_device_users(device_path)

        def _on_done(users: list[str]) -> None:
            if users:
                self.emit("device-busy", device_path, users)
            else:
                self.emit("error", _("Failed to start camera stream."))

        threading.Thread(
            target=lambda: GLib.idle_add(_on_done, _worker()),
            daemon=True,
        ).start()

    # -- virtual camera output (appsrc -> v4l2sink) --------------------------

    def _start_vcam(self, device: str) -> None:
        """Prepare appsrc -> v4l2sink pipeline for virtual camera output.

        The pipeline is created lazily on the first frame when resolution is known.
        Pipeline creation MUST happen on the GLib main thread to avoid deadlocks
        with GStreamer's internal mutexes (probe callbacks run on streaming threads).
        """
        self._vcam_device = device
        self._vcam_building = False
        self._vcam_pending_frame = None
        log.info("Virtual camera output prepared on %s", device)

    def _rebuild_vcam(self, w: int, h: int) -> None:
        """(Re)create the virtual camera pipeline with correct resolution.

        MUST be called on the GLib main thread (not from a GStreamer probe).
        """
        self._stop_vcam()
        self._vcam_building = False
        device = self._vcam_device
        if not device:
            return
        # Limit appsrc internal buffering to ~2 frames to prevent OOM if the
        # downstream v4l2sink stalls or rejects frames.
        max_bytes = w * h * 4 * 2  # 2 BGRA frames
        pipeline_str = (
            f"appsrc name=src emit-signals=false is-live=true format=time block=false max-bytes={max_bytes} max-buffers=2 leaky-type=downstream do-timestamp=true "
            f"caps=video/x-raw,format=BGRA,width={w},height={h},framerate=30/1 "
            f"! queue max-size-buffers=2 leaky=downstream silent=true "
            f"! videoconvert n-threads={min(os.cpu_count() or 2, 4)} "
            "! video/x-raw,format=YUY2 "
            f"! v4l2sink device={device} sync=false"
        )
        log.info("Building vcam pipeline: %s", pipeline_str)
        try:
            self._vcam_pipeline = Gst.parse_launch(pipeline_str)
        except GLib.Error as e:
            log.error("Failed to create vcam pipeline: %s", e)
            return
        self._vcam_appsrc = self._vcam_pipeline.get_by_name("src")
        self._vcam_w = w
        self._vcam_h = h
        ret = self._vcam_pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            log.error("vcam pipeline failed to start on %s - cleaning up", device)
            self._vcam_pipeline.set_state(Gst.State.NULL)
            self._vcam_pipeline = None
            self._vcam_appsrc = None
            self._vcam_w = 0
            self._vcam_h = 0
            self._vcam_device = ""
            return
        log.info("Virtual camera started on %s (%dx%d) state=%s", device, w, h, ret)
        # Drain the frame that was queued while we were building
        pending = self._vcam_pending_frame
        self._vcam_pending_frame = None
        if pending and self._vcam_appsrc:
            self._push_vcam(*pending)

    def _rebuild_vcam_idle(self, w: int, h: int) -> bool:
        """GLib idle callback: create vcam pipeline on the main thread."""
        self._rebuild_vcam(w, h)
        return False  # Run only once

    def _stop_vcam(self) -> None:
        """Stop the virtual camera pipeline."""
        if self._vcam_pipeline:
            log.info("Stopping vcam pipeline")
            self._vcam_pipeline.set_state(Gst.State.NULL)
            self._vcam_pipeline = None
            self._vcam_appsrc = None
            self._vcam_w = 0
            self._vcam_h = 0

    def _release_vcam_device(self) -> None:
        """Release the VirtualCamera allocation for the vcam output."""
        if self._vcam_alloc_id:
            VirtualCamera.release_device(self._vcam_alloc_id)
            self._vcam_alloc_id = ""

    def _schedule_vcam_push(self, bgra_bytes: bytes, w: int, h: int) -> None:
        """Store frame and schedule async push to vcam on the main thread.

        Called from GStreamer probe (streaming thread). Decouples vcam
        push-buffer from the probe to prevent pipeline stalls if
        v4l2sink causes backpressure.
        """
        self._vcam_latest_frame = (bgra_bytes, w, h)
        if not self._vcam_idle_scheduled:
            self._vcam_idle_scheduled = True
            GLib.idle_add(self._vcam_idle_push)

    def _vcam_idle_push(self) -> bool:
        """GLib idle callback: push latest vcam frame."""
        self._vcam_idle_scheduled = False
        frame = self._vcam_latest_frame
        self._vcam_latest_frame = None
        if frame:
            self._push_vcam(*frame)
        return False  # Run only once

    def _push_vcam(self, bgra_bytes: bytes, w: int, h: int) -> None:
        """Push a BGRA frame to the virtual camera appsrc.

        Safe to call from any thread. Pipeline creation is delegated to the
        GLib main thread the first time this is called (lazy init).
        """
        if not self._vcam_device:
            return

        # Pipeline not ready yet: schedule creation on the main thread and
        # stash the most-recent frame so it can be sent once the pipeline starts.
        if self._vcam_w == 0:
            self._vcam_pending_frame = (bgra_bytes, w, h)
            if not self._vcam_building:
                self._vcam_building = True
                # Force even dimensions (YUY2/I420 requirement)
                vcam_w = w if w % 2 == 0 else w + 1
                vcam_h = h if h % 2 == 0 else h + 1
                GLib.idle_add(self._rebuild_vcam_idle, vcam_w, vcam_h)
            return

        appsrc = self._vcam_appsrc
        if not appsrc:
            return
        # Resize if resolution changed
        if w != self._vcam_w or h != self._vcam_h:
            try:
                arr = np.frombuffer(bgra_bytes, dtype=np.uint8).reshape((h, w, 4))
                arr = cv2.resize(arr, (self._vcam_w, self._vcam_h))
                bgra_bytes = arr.tobytes()
            except Exception:
                return
        buf = Gst.Buffer.new_wrapped(bgra_bytes)
        ret = appsrc.emit("push-buffer", buf)
        if ret != Gst.FlowReturn.OK:
            log.warning("vcam push-buffer returned %s - stopping vcam", ret)
            self._stop_vcam()
            self._release_vcam_device()
            self._vcam_device = ""

    def _promote_vcam_to_background(self, camera):
        device = self._vcam_device
        self._stop_vcam()
        self._vcam_alloc_id = ""
        self._vcam_device = ""
        if not device or not VirtualCamera.is_enabled():
            VirtualCamera.release_device(camera.id)
            return
        if camera.backend == BackendType.PHONE:
            from core.frame_output import FrameOutput
            server = camera.extra.get("phone_server")
            if server:
                self._bg_phone_server_ref = server
                self._bg_phone_cam_id = camera.id
                self._bg_phone_output = FrameOutput(device)
                server.set_frame_callback(self._on_phone_frame_bg)
            else:
                VirtualCamera.release_device(camera.id)
        else:
            self.ensure_bg_vcam(camera)

    def _create_bg_vcam_pipeline(self, cam_id, camera, device):
        # Compatibility callback: actual allocation/setup belongs to a bounded worker.
        self.ensure_bg_vcam(camera)
        return GLib.SOURCE_REMOVE

    def _stop_bg_vcam(self, camera_id):
        pipe = self._bg_vcam_pipelines.pop(camera_id, None)
        if pipe:
            pipe.get_bus().remove_signal_watch()
            pipe.set_state(Gst.State.NULL)
        feeder = self._bg_vcam_feeders.pop(camera_id, None)
        if feeder:
            feeder.stop()
        if self._bg_phone_cam_id == camera_id:
            self._stop_bg_phone_vcam()
        if not self._current_camera or self._current_camera.id != camera_id:
            VirtualCamera.release_device(camera_id)

    def stop_all_bg_vcams(self):
        self._bg_generation += 1
        for camera_id in set(self._bg_vcam_pipelines) | set(self._bg_vcam_feeders):
            self._stop_bg_vcam(camera_id)
        self._stop_bg_phone_vcam()

    def has_active_bg_vcams(self):
        return bool(self._bg_vcam_pipelines or self._bg_vcam_feeders or self._bg_phone_cam_id)

    @property
    def vcam_active(self) -> bool:
        """Return True if the foreground virtual camera output is active."""
        return bool(self._vcam_device)

    def stop_vcam(self) -> None:
        """Public wrapper: stop foreground vcam and release the device."""
        self._stop_vcam()
        self._release_vcam_device()
        self._vcam_device = ""

    def ensure_bg_vcam(self, camera):
        if (not VirtualCamera.is_enabled() or camera.id in self._settings.get("vcam-disabled-cameras", [])
                or self._current_camera and self._current_camera.id == camera.id
                or camera.id in self._bg_vcam_pipelines or camera.id in self._bg_vcam_feeders
                or camera.id in self._bg_pending or self._bg_phone_cam_id == camera.id):
            return GLib.SOURCE_REMOVE
        # Browser frames have an explicit producer callback, installed on promotion.
        # Do not turn on an unselected DSLR just because USB discovery found it.
        if camera.backend in (BackendType.PHONE, BackendType.GPHOTO2):
            backend = self._manager.get_backend(camera.backend)
            if camera.backend == BackendType.PHONE or not backend or not backend.is_camera_streaming(camera):
                return GLib.SOURCE_REMOVE
        generation = self._bg_generation
        self._bg_pending.add(camera.id)
        def create():
            device = VirtualCamera.ensure_ready(camera_id=camera.id)
            if not device:
                raise RuntimeError("No authorized virtual camera output is available")
            if camera.device_path == device:
                raise RuntimeError("Virtual output cannot read itself")
            source = self._manager.get_gst_source(camera, prefer_v4l2=True)
            if not source:
                raise RuntimeError("Camera has no streaming source")
            pipe = Gst.parse_launch(
                f"{source} ! queue max-size-buffers=2 leaky=downstream ! videoconvert ! "
                f"video/x-raw,format=YUY2 ! v4l2sink device={gst_quote(device)} sync=false")
            if pipe.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                pipe.set_state(Gst.State.NULL)
                raise RuntimeError("Background camera pipeline failed")
            return pipe
        def done(pipe):
            self._bg_pending.discard(camera.id)
            is_active = self._current_camera and self._current_camera.id == camera.id
            exists = any(cam.id == camera.id for cam in self._manager.cameras)
            if generation != self._bg_generation or is_active or not exists or not VirtualCamera.is_enabled():
                pipe.set_state(Gst.State.NULL)
                if not is_active:
                    VirtualCamera.release_device(camera.id)
                return
            self._bg_vcam_pipelines[camera.id] = pipe
            bus = pipe.get_bus()
            bus.add_signal_watch()
            bus.connect("message::error", lambda *_: self._stop_bg_vcam(camera.id))
            bus.connect("message::eos", lambda *_: self._stop_bg_vcam(camera.id))
        def failed(exc):
            self._bg_pending.discard(camera.id)
            if not self._current_camera or self._current_camera.id != camera.id:
                VirtualCamera.release_device(camera.id)
            log.warning("Background camera setup failed: %s", exc)
            # Retry is explicit, not an unbounded timer or repeated Polkit prompt.
        run_async(create, on_success=done, on_error=failed)
        return GLib.SOURCE_REMOVE

    # -- phone camera --------------------------------------------------------

    _phone_server_ref: Any = None
    _phone_frame_pending: bool = False
    _phone_v4l2_pipeline: Any = None  # GStreamer appsrc -> v4l2sink for virtual cam
    _phone_v4l2_appsrc: Any = None
    _phone_v4l2_caps_set: bool = False
    _phone_v4l2_device: str = ""
    _phone_v4l2_w: int = 0
    _phone_v4l2_h: int = 0
    _phone_v4l2_building: bool = False
    _phone_v4l2_pending_frame: Any = None  # (bgr_ndarray, w, h) or None

    # Background phone vcam forwarding (when phone camera is not active but
    # virtual camera should keep outputting frames)
    _bg_phone_server_ref: Any = None
    _bg_phone_cam_id: str = ""

    def _start_phone_camera(self, camera: CameraInfo) -> bool:
        server = camera.extra.get("phone_server")
        if not server:
            self.emit("error", _("Phone camera server not available."))
            return False
        self._stop_bg_phone_vcam()
        self._use_appsink = True
        self._phone_server_ref = server
        generation = self._generation
        server.set_frame_callback(lambda frame: self._on_phone_frame(frame, generation))
        self._start_fps_counter()
        return True

    def _start_phone_v4l2(self, device: str) -> None:
        """Create appsrc -> videoconvert -> v4l2sink pipeline for phone -> virtual camera."""
        self._phone_v4l2_device = device
        self._phone_v4l2_building = False
        self._phone_v4l2_pending_frame = None
        # Pipeline will be created on first frame when we know the resolution

    def _rebuild_phone_v4l2(self, w: int, h: int) -> None:
        """(Re)create the v4l2 pipeline with correct resolution.

        MUST be called on the GLib main thread (not from an asyncio callback).
        """
        self._stop_phone_v4l2()
        self._phone_v4l2_building = False
        device = self._phone_v4l2_device
        if not device:
            log.warning("Cannot rebuild phone v4l2: no device set")
            return
        pipeline_str = (
            "appsrc name=src emit-signals=false is-live=true format=time block=false max-buffers=2 leaky-type=downstream do-timestamp=true "
            f"caps=video/x-raw,format=BGR,width={w},height={h},framerate=30/1 "
            f"! videoconvert n-threads={min(os.cpu_count() or 2, 4)} "
            "! video/x-raw,format=YUY2 "
            f"! v4l2sink device={device} sync=false"
        )
        log.info("Building phone v4l2 pipeline: %s", pipeline_str)
        try:
            self._phone_v4l2_pipeline = Gst.parse_launch(pipeline_str)
        except GLib.Error as e:
            log.error("Failed to create phone v4l2 pipeline: %s", e)
            return
        self._phone_v4l2_appsrc = self._phone_v4l2_pipeline.get_by_name("src")
        self._phone_v4l2_w = w
        self._phone_v4l2_h = h
        self._phone_v4l2_caps_set = True
        ret = self._phone_v4l2_pipeline.set_state(Gst.State.PLAYING)
        log.info(
            "Phone virtual camera output started on %s (%dx%d) state=%s",
            device,
            w,
            h,
            ret,
        )
        # Drain the frame queued while building
        pending = self._phone_v4l2_pending_frame
        self._phone_v4l2_pending_frame = None
        if pending is not None and self._phone_v4l2_appsrc:
            bgr_p, wp, hp = pending
            self._push_phone_v4l2(bgr_p, wp, hp)

    def _rebuild_phone_v4l2_idle(self, w: int, h: int) -> bool:
        """GLib idle callback: create phone v4l2 pipeline on the main thread."""
        self._rebuild_phone_v4l2(w, h)
        return False  # Run only once

    def _stop_phone_v4l2(self) -> None:
        """Stop the phone virtual camera pipeline."""
        if self._phone_v4l2_pipeline:
            log.info("Stopping phone v4l2 pipeline")
            self._phone_v4l2_pipeline.set_state(Gst.State.NULL)
            self._phone_v4l2_pipeline = None
            self._phone_v4l2_appsrc = None
            self._phone_v4l2_caps_set = False
            self._phone_v4l2_w = 0
            self._phone_v4l2_h = 0

    def _push_phone_v4l2(self, bgr, w: int, h: int) -> None:
        """Push a BGR frame to the appsrc for virtual camera output.

        When the phone rotates, the frame is resized to match the original
        pipeline resolution to avoid recreating the v4l2sink (which causes
        OBS to drop the device).

        Safe to call from asyncio threads. Pipeline creation is delegated to
        the GLib main thread on first call (lazy init).
        """
        if not self._phone_v4l2_device:
            return
        # First frame: schedule pipeline creation on the main thread
        if not self._phone_v4l2_caps_set:
            self._phone_v4l2_pending_frame = (bgr, w, h)
            if not self._phone_v4l2_building:
                self._phone_v4l2_building = True
                GLib.idle_add(self._rebuild_phone_v4l2_idle, w, h)
            return
        appsrc = self._phone_v4l2_appsrc
        if not appsrc:
            return
        # Resize if resolution changed (rotation) to keep pipeline stable
        pw, ph = self._phone_v4l2_w, self._phone_v4l2_h
        if w != pw or h != ph:
            log.info("Phone v4l2: resizing frame %dx%d -> %dx%d", w, h, pw, ph)
            bgr = cv2.resize(bgr, (pw, ph))
        data = bytes(bgr.data)
        expected = pw * ph * 3
        if len(data) != expected:
            log.warning(
                "Phone v4l2: buffer size mismatch: got %d, expected %d",
                len(data),
                expected,
            )
            return
        buf = Gst.Buffer.new_wrapped(data)
        ret = appsrc.emit("push-buffer", buf)
        if ret != Gst.FlowReturn.OK:
            log.warning("Phone v4l2: push-buffer returned %s", ret)

    def _on_phone_frame(self, bgr: Any, generation: int | None = None) -> None:
        if generation is None:
            generation = self._generation
        if self._current_camera is None or generation != self._generation:
            return
        bgr = self._apply_frame_processing(bgr)
        if generation != self._generation:
            return
        h, w = bgr.shape[:2]
        # Mirror Preview is a display preference, not a destructive media transform.
        self._frame_count += 1
        self._distribute_processed_frame(bgr, w, h)
        self._notify_first_frame(generation)
        self._queue_texture(bgr, generation)

    def _update_phone_texture(
        self, w: int, h: int, stride: int, glib_bytes: GLib.Bytes
    ) -> bool:
        self._phone_frame_pending = False
        try:
            texture = Gdk.MemoryTexture.new(
                w, h, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED, glib_bytes, stride
            )
            self._last_texture = texture
            self.emit("new-texture", texture)
        except Exception:
            pass
        return False

    def _on_phone_frame_bg(self, bgr):
        output = getattr(self, "_bg_phone_output", None)
        if output is not None:
            output.push(bgr)

    def _stop_bg_phone_vcam(self):
        if self._bg_phone_server_ref is not None:
            self._bg_phone_server_ref.set_frame_callback(None)
            self._bg_phone_server_ref = None
        output = getattr(self, "_bg_phone_output", None)
        self._bg_phone_output = None
        if output:
            output.stop()
        if self._bg_phone_cam_id:
            VirtualCamera.release_device(self._bg_phone_cam_id)
            self._bg_phone_cam_id = ""

    # -- bus handling --------------------------------------------------------

    def _on_bus_message(self, _bus: Gst.Bus, msg: Gst.Message) -> None:
        if msg.type == Gst.MessageType.EOS:
            log.info("Stream reached end-of-stream.")
            self.stop()
        elif msg.type == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            error_text = err.message if err else _("Unknown GStreamer error")
            log.error("GStreamer error: %s (debug: %s)", error_text, dbg)

            # Save device_path before stop() clears _current_camera
            dev_path = (
                self._current_camera.device_path
                if self._current_camera
                else ""
            )

            combined = (error_text + (dbg or "")).lower()
            busy = any(
                kw in combined
                for kw in (
                    "resource busy", "busy", "ebusy",
                    "cannot open", "ocupado", "alocar",
                    "allocate", "buffer pool",
                )
            )
            if busy and dev_path:
                # If the device was just released from a bg vcam, retry once
                # after additional delay instead of giving up immediately.
                retry = getattr(self, "_play_busy_retries", 0)
                if retry < 2 and self._current_camera:
                    self._play_busy_retries = retry + 1
                    cam = self._current_camera
                    fmt = self._current_fmt
                    log.info("Device %s busy - retry %d/2 in 500ms", dev_path, retry + 1)
                    self.stop(stop_backend=False)
                    self._current_camera = cam
                    self._current_fmt = fmt
                    GLib.timeout_add(500, self._play_continue, cam, fmt, False)
                    return
                self._play_busy_retries = 0
                users = _find_device_users(dev_path)
                if users:
                    self.stop()
                    self.emit("device-busy", dev_path, users)
                    return

            # PipeWire async failure (e.g. unhandled format): retry with v4l2src
            if self._try_pw_fallback():
                return

            # Even without explicit busy keywords, check if the device is
            # actually held by another process before reporting a generic error.
            if dev_path:
                users = _find_device_users(dev_path)
                if users:
                    self.stop()
                    self.emit("device-busy", dev_path, users)
                    return

            self.stop()
            self.emit("error", error_text)
        elif msg.type == Gst.MessageType.WARNING:
            err, dbg = msg.parse_warning()
            wmsg = err.message if err else ""
            # Suppress expected leaky queue warnings
            if "descartada" not in wmsg and "dropping" not in wmsg.lower():
                log.warning("GStreamer warning: %s", wmsg)

    def _try_pw_fallback(self) -> bool:
        """If the current pipeline uses pipewiresrc, retry with v4l2src.

        Returns True if fallback succeeded and streaming continues.
        """
        camera = self._current_camera
        if not camera or not camera.device_path:
            return False
        # Only fallback if the failing pipeline uses pipewiresrc
        if not self._pipeline:
            return False
        pipe_str = self._pipeline.get_name()
        has_pw = False
        it = self._pipeline.iterate_sources()
        while True:
            ret, elem = it.next()
            if ret == Gst.IteratorResult.OK:
                factory = elem.get_factory()
                if factory and factory.get_name() == "pipewiresrc":
                    has_pw = True
                    break
            else:
                break
        if not has_pw:
            return False

        log.warning(
            "PipeWire pipeline failed async for %s, retrying with v4l2src",
            camera.device_path,
        )
        # Save loopback state before stop
        loopback_device = self._vcam_device
        self.stop()

        backend = self._manager.get_backend(camera.backend)
        if not backend or not hasattr(backend, "_v4l2_gst_source"):
            return False

        fmt_obj = None
        if camera.formats and hasattr(backend, "_pick_best_format"):
            fmt_obj = backend._pick_best_format(camera)
        v4l2_source = backend._v4l2_gst_source(
            camera.device_path, camera, fmt_obj
        )
        n_threads = min(os.cpu_count() or 2, 4)
        suffix = (
            f"videoconvert n-threads={n_threads} name=conv ! "
            f"video/x-raw,format=BGRA ! "
            f"tee name=t ! "
            f"queue max-size-buffers=4 leaky=downstream silent=true ! "
            f"gtk4paintablesink sync=true max-lateness=-1 qos=false"
        )
        fallback_pipeline = f"{v4l2_source} ! {suffix}"
        if self._try_start_paintable(fallback_pipeline):
            self._current_camera = camera
            if camera.device_path:
                self._disable_usb_autosuspend(camera.device_path)
            if loopback_device:
                self._start_vcam(loopback_device)
            return True
        return False

    @staticmethod
    def _read_bgra_buffer(buffer, caps):
        video = GstVideo.VideoInfo.new_from_caps(caps)
        if video.finfo.name != "BGRA":
            raise ValueError("The processing branch must negotiate BGRA")
        meta = GstVideo.buffer_get_video_meta(buffer)
        stride = meta.stride[0] if meta else video.stride[0]
        offset = meta.offset[0] if meta else video.offset[0]
        ok, mapping = buffer.map(Gst.MapFlags.READ)
        if not ok:
            raise ValueError("Could not map video buffer")
        try:
            return bgr_from_bgra(mapping.data, video.width, video.height, stride, offset)
        finally:
            buffer.unmap(mapping)

    def _notify_first_frame(self, generation):
        if generation != self._generation or self._has_received_frame:
            return
        self._has_received_frame = True
        def announce():
            if generation == self._generation and self._current_camera is not None:
                self.emit("state-changed", "playing")
                self._resolve_vcam_async()
            return GLib.SOURCE_REMOVE
        GLib.idle_add(announce)

    def _queue_texture(self, bgr, generation):
        h, w = bgr.shape[:2]
        data = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA).tobytes()
        if self._texture_slot.publish((generation, w, h, data)):
            GLib.idle_add(self._flush_texture)

    def _flush_texture(self):
        value = self._texture_slot.take()
        if value is not None:
            generation, w, h, data = value
            if generation == self._generation and self._current_camera is not None:
                self._update_texture(w, h, w * 4, GLib.Bytes.new(data))
        return GLib.SOURCE_REMOVE
