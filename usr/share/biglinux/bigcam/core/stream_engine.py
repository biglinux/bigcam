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

from gi.repository import Gst, Gdk, GLib, GObject

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

Gst.init(None)
log = logging.getLogger(__name__)

# Backends that stream via UDP (MPEG-TS) need appsink
_APPSINK_BACKENDS = {BackendType.GPHOTO2, BackendType.IP}


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
    and pushes them to a v4l2loopback device via GStreamer appsrc → v4l2sink.
    Runs in a daemon thread — safe to abandon on app exit.
    """

    def __init__(self, device_path: str, loopback_device: str, camera_name: str) -> None:
        self._device_path = device_path
        self._loopback = loopback_device
        self._name = camera_name
        self._stop = threading.Event()
        self._cap: Any = None
        self._pipeline: Gst.Pipeline | None = None
        self._appsrc: Any = None
        self._thread: threading.Thread | None = None
        self._w = 0
        self._h = 0

    def start(self) -> bool:
        """Open the camera and start the feeder thread. Returns True on success."""
        if not _HAS_CV2:
            return False
        cap = cv2.VideoCapture(self._device_path, cv2.CAP_V4L2)
        if not cap.isOpened():
            log.warning("BgVcamFeeder: failed to open %s", self._device_path)
            cap.release()
            return False
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc('M', 'J', 'P', 'G'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        cap.set(cv2.CAP_PROP_FPS, 30)
        ret, frame = cap.read()
        if not ret:
            log.warning("BgVcamFeeder: failed to read test frame from %s", self._device_path)
            cap.release()
            return False
        self._cap = cap
        self._h, self._w = frame.shape[:2]
        # Build appsrc → v4l2sink pipeline
        nthreads = min(os.cpu_count() or 2, 4)
        max_bytes = self._w * self._h * 4 * 2  # 2 BGRA frames
        pipeline_str = (
            f"appsrc name=src emit-signals=false is-live=true format=time block=false max-bytes={max_bytes} "
            f"caps=video/x-raw,format=BGRA,width={self._w},height={self._h},framerate=30/1 "
            f"! queue max-size-buffers=2 leaky=downstream silent=true "
            f"! videoconvert n-threads={nthreads} "
            "! video/x-raw,format=YUY2 "
            f"! v4l2sink device={self._loopback} sync=false"
        )
        try:
            self._pipeline = Gst.parse_launch(pipeline_str)
        except GLib.Error as e:
            log.error("BgVcamFeeder: pipeline parse error: %s", e)
            cap.release()
            self._cap = None
            return False
        self._appsrc = self._pipeline.get_by_name("src")
        ret_state = self._pipeline.set_state(Gst.State.PLAYING)
        if ret_state == Gst.StateChangeReturn.FAILURE:
            log.warning("BgVcamFeeder: pipeline failed to start for %s", self._name)
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
            cap.release()
            self._cap = None
            return False
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name=f"bgvcam-{self._name}",
        )
        self._thread.start()
        log.info(
            "BgVcamFeeder started: %s → %s (%dx%d)",
            self._device_path, self._loopback, self._w, self._h,
        )
        return True

    def _loop(self) -> None:
        """Background capture → push loop."""
        cap = self._cap
        appsrc = self._appsrc
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        orig_stderr_fd = os.dup(2)
        try:
            while cap is not None and cap.isOpened() and not self._stop.is_set():
                os.dup2(devnull_fd, 2)
                ret, frame = cap.read()
                os.dup2(orig_stderr_fd, 2)
                if not ret:
                    if self._stop.is_set():
                        break
                    continue
                bgra = cv2.cvtColor(frame, cv2.COLOR_BGR2BGRA)
                h, w = bgra.shape[:2]
                if w != self._w or h != self._h:
                    bgra = cv2.resize(bgra, (self._w, self._h))
                buf = Gst.Buffer.new_wrapped(bgra.tobytes())
                if appsrc:
                    ret = appsrc.emit("push-buffer", buf)
                    if ret != Gst.FlowReturn.OK:
                        log.warning("BgVcamFeeder: push-buffer returned %s — stopping", ret)
                        break
        finally:
            os.dup2(orig_stderr_fd, 2)
            os.close(devnull_fd)
            os.close(orig_stderr_fd)

    def stop(self) -> None:
        """Stop the feeder and release resources."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        if self._pipeline is not None:
            self._pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
            self._appsrc = None
        log.info("BgVcamFeeder stopped: %s", self._name)


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
        # General virtual camera output (appsrc → v4l2sink)
        self._vcam_pipeline: Gst.Pipeline | None = None
        self._vcam_appsrc: Any = None
        self._vcam_device: str = ""
        self._vcam_alloc_id: str = ""  # VirtualCamera allocation id for vcam device
        self._vcam_w: int = 0
        self._vcam_h: int = 0
        self._vcam_bgra_buf: np.ndarray | None = None
        self._prefer_v4l2: bool = False  # bypass PipeWire, use v4l2src directly
        # OpenCV direct capture (like guvcview) — used when prefer_v4l2 is active
        self._cv_cap: Any = None  # cv2.VideoCapture or None
        self._cv_timer_id: int | None = None  # GLib.timeout_add ID for frame poll
        # Flag: True while _rebuild_vcam is scheduled/running on the main thread.
        # Prevents multiple concurrent rebuild requests from the probe thread.
        self._vcam_building: bool = False
        # Most-recent frame queued while the vcam pipeline is being built.
        # Tuple of (bgra_bytes, w, h) or None.
        self._vcam_pending_frame: tuple | None = None
        # Latest frame for async vcam push (set by probe, consumed by idle)
        self._vcam_latest_frame: tuple | None = None
        self._vcam_idle_scheduled: bool = False
        # Background virtual camera pipelines (camera_id → pipeline)
        self._bg_vcam_pipelines: dict[str, Gst.Pipeline] = {}
        # Background OpenCV-based vcam feeders (camera_id → _BgVcamFeeder)
        self._bg_vcam_feeders: dict[str, _BgVcamFeeder] = {}


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

    def set_sharpness(self, level: float) -> None:
        """Set software sharpness (0.0 = off, up to 1.0 = max)."""
        self._sharpness = max(0.0, min(1.0, level))

    def set_pan(self, value: float) -> None:
        """Set software pan offset (-1.0 left .. 1.0 right)."""
        self._pan = max(-1.0, min(1.0, value))

    def set_tilt(self, value: float) -> None:
        """Set software tilt offset (-1.0 up .. 1.0 down)."""
        self._tilt = max(-1.0, min(1.0, value))

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
        self._current_fps = self._frame_count
        self._frame_count = 0
        return True

    def _on_frame_probe(
        self, pad: Gst.Pad, info: Gst.PadProbeInfo
    ) -> Gst.PadProbeReturn:
        self._frame_count += 1
        return Gst.PadProbeReturn.OK

    # -- shared frame processing ---------------------------------------------

    def _apply_frame_processing(self, bgr: np.ndarray) -> np.ndarray:
        """Apply all software effects to a BGR frame (effects, zoom, sharpness,
        QR overlay)."""
        if self._effects.has_active_effects():
            bgr = self._effects.apply(bgr)
        # Digital zoom + pan/tilt
        if self._zoom_level > 1.0 or self._pan != 0.0 or self._tilt != 0.0:
            zh, zw = bgr.shape[:2]
            zoom = self._zoom_level
            if (self._pan != 0.0 or self._tilt != 0.0) and zoom < 1.5:
                zoom = 1.5
            crop_h = int(zh / zoom)
            crop_w = int(zw / zoom)
            cx = zw // 2 + int(self._pan * (zw - crop_w) / 2)
            cy = zh // 2 + int(self._tilt * (zh - crop_h) / 2)
            x0 = max(0, min(cx - crop_w // 2, zw - crop_w))
            y0 = max(0, min(cy - crop_h // 2, zh - crop_h))
            cropped = bgr[y0:y0 + crop_h, x0:x0 + crop_w]
            bgr = cv2.resize(cropped, (zw, zh), interpolation=cv2.INTER_LINEAR)
        # Software sharpness: unsharp mask
        if self._sharpness > 0.0:
            blurred = cv2.GaussianBlur(bgr, (0, 0), 3)
            amount = self._sharpness * 2.0
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
        """Store processed frame (with mirror) and feed to vcam/recorder.

        When *bgra_direct* is provided (fast path), push it straight to the
        virtual camera without an extra BGR→BGRA conversion.
        """
        self._last_probe_bgr = cv2.flip(bgr, 1) if self._mirror else bgr
        if self._vcam_device and self._last_probe_bgr is not None:
            if bgra_direct is not None and not self._mirror:
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

    def _on_paintable_probe(
        self, pad: Gst.Pad, info: Gst.PadProbeInfo
    ) -> Gst.PadProbeReturn:
        """Buffer probe on tee sink — applies OpenCV effects via buffer replacement."""
        self._frame_count += 1

        has_work = self._has_processing_work()
        is_recording = self._video_recorder and self._video_recorder.is_recording
        # Fast path: no effects/overlays — only grab BGR every 10th frame for photos
        # BUT if virtual camera is active, process every frame for smooth output
        if not has_work and not is_recording and not self._vcam_device and self._frame_count % 10 != 0:
            return Gst.PadProbeReturn.OK

        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        caps = pad.get_current_caps()
        if caps is None:
            return Gst.PadProbeReturn.OK
        s = caps.get_structure(0)
        w = s.get_value("width")
        h = s.get_value("height")
        # Cache format string — it never changes during a pipeline's lifetime
        if not self._probe_cached_fmt:
            self._probe_cached_fmt = s.get_string("format") or ""
        fmt = self._probe_cached_fmt
        self._probe_debug_count += 1
        if self._probe_debug_count <= 3:
            log.debug(f"paintable_probe: fmt={fmt}, {w}x{h}")

        ok, map_info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.PadProbeReturn.OK
        bgr = None
        result = None
        try:
            # Use numpy view directly on mapped buffer — no bytes() copy
            raw_arr = np.frombuffer(map_info.data, dtype=np.uint8)
            if fmt in ("BGRA", "BGRx"):
                frame = raw_arr.reshape((h, w, 4))
                bgr = frame[:, :, :3]  # View, no copy yet
            elif fmt == "BGR":
                bgr = raw_arr.reshape((h, w, 3))  # View
            elif fmt == "RGB":
                rgb = raw_arr.reshape((h, w, 3))
                bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            elif fmt == "I420":
                yuv = raw_arr.reshape((h * 3 // 2, w))
                bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
            elif fmt == "NV12":
                yuv = raw_arr.reshape((h * 3 // 2, w))
                bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)
            elif fmt in ("YUY2", "YUYV"):
                yuv = raw_arr.reshape((h, w * 2))
                bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_YUY2)
            if bgr is not None:
                if has_work:
                    # Single copy for processing; views become owned arrays
                    processed = self._apply_frame_processing(bgr.copy())
                    self._distribute_processed_frame(processed, w, h)
                    # Convert back to original GStreamer pipeline format
                    if fmt in ("BGRA", "BGRx"):
                        out = np.empty((h, w, 4), dtype=np.uint8)
                        out[:, :, :3] = processed
                        out[:, :, 3] = frame[:, :, 3]  # Keep original alpha
                        result = out.tobytes()
                    elif fmt == "BGR":
                        result = processed.tobytes()
                    elif fmt == "RGB":
                        result = cv2.cvtColor(processed, cv2.COLOR_BGR2RGB).tobytes()
                    elif fmt == "I420":
                        result = cv2.cvtColor(
                            processed, cv2.COLOR_BGR2YUV_I420
                        ).tobytes()
                    elif fmt == "NV12":
                        # OpenCV has no BGR→NV12; convert to I420 then rearrange
                        i420 = cv2.cvtColor(processed, cv2.COLOR_BGR2YUV_I420)
                        flat = i420.ravel()
                        y_sz = h * w
                        uv_sz = y_sz // 4
                        nv12 = np.empty(y_sz + uv_sz * 2, dtype=np.uint8)
                        nv12[:y_sz] = flat[:y_sz]
                        nv12[y_sz::2] = flat[y_sz : y_sz + uv_sz]
                        nv12[y_sz + 1 :: 2] = flat[y_sz + uv_sz :]
                        result = nv12.tobytes()
                    elif fmt in ("YUY2", "YUYV"):
                        if hasattr(cv2, "COLOR_BGR2YUV_YUY2"):
                            result = cv2.cvtColor(
                                processed, cv2.COLOR_BGR2YUV_YUY2
                            ).tobytes()
                else:
                    # No effects — fast path: minimise copies
                    is_rec = self._video_recorder and self._video_recorder.is_recording
                    need_bgr = is_rec or (self._frame_count % 10 == 0)
                    bgr_copy = bgr.copy() if need_bgr else None
                    if self._vcam_device and fmt in ("BGRA", "BGRx"):
                        bgra_direct = bytes(map_info.data)
                        if bgr_copy is not None:
                            self._distribute_processed_frame(
                                bgr_copy, w, h, bgra_direct=bgra_direct,
                            )
                        else:
                            # Vcam only — skip BGR entirely
                            if self._mirror:
                                arr = np.frombuffer(bgra_direct, dtype=np.uint8).reshape((h, w, 4))
                                self._schedule_vcam_push(cv2.flip(arr, 1).tobytes(), w, h)
                            else:
                                self._schedule_vcam_push(bgra_direct, w, h)
                    elif bgr_copy is not None:
                        self._distribute_processed_frame(bgr_copy, w, h)
        except Exception as e:
            if self._probe_debug_count <= 5:
                log.debug(f"paintable_probe error: {e}")
        finally:
            buf.unmap(map_info)
        if result is not None:
            new_buf = Gst.Buffer.new_wrapped(result)
            new_buf.pts = buf.pts
            new_buf.dts = buf.dts
            new_buf.duration = buf.duration
            new_buf.offset = buf.offset
            info.set_buffer(new_buf)
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
        """Save the current preview frame as a PNG file.

        Works for both paintable and appsink pipelines.
        Prioritizes the probe's BGR frame which has all effects and mirroring applied.
        """
        # 1. Try capture from probe's last frame (includes all effects + mirror)
        if self._last_probe_bgr is not None:
            try:
                cv2.imwrite(output_path, self._last_probe_bgr)
                return True
            except Exception as exc:
                log.error("Failed to save probe snapshot: %s", exc)
                # Fall through to fallback methods

        # 2. Appsink pipeline fallback: stores last texture directly
        if self._use_appsink and self._last_texture:
            try:
                self._last_texture.save_to_png(output_path)
                return True
            except Exception as exc:
                log.error("Failed to save appsink snapshot: %s", exc)

        # 3. Last resort: try paintable directly
        if self._gtksink:
            paintable = self._gtksink.get_property("paintable")
            if paintable and hasattr(paintable, "save_to_png"):
                try:
                    paintable.save_to_png(output_path)
                    return True
                except Exception:
                    pass
        return False

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
        # If this camera had a background vcam (GStreamer or OpenCV feeder),
        # stop it — we'll create a new effects-aware one. The bg source holds
        # an exclusive lock on the device; after stopping, allow the kernel a
        # moment to release it before opening again.
        had_bg_vcam = (
            camera.id in self._bg_vcam_pipelines
            or camera.id in self._bg_vcam_feeders
        )
        self._stop_bg_vcam(camera.id)
        if had_bg_vcam:
            # Device needs a moment to be fully released — defer the rest
            GLib.timeout_add(300, self._play_continue, camera, fmt, streaming_ready)
            return True
        return self._play_continue(camera, fmt, streaming_ready)

    def _play_continue(self, camera: CameraInfo, fmt: VideoFormat | None, streaming_ready: bool) -> bool:
        """Continuation of play() — may be deferred via GLib.timeout_add.
        Always returns False so GLib.timeout_add won't repeat."""
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
                # For gphoto2: allocate v4l2loopback device BEFORE streaming
                # so ffmpeg can write directly to it (survives camera switches
                # and app close with "Keep camera on")
                if camera.backend == BackendType.GPHOTO2:
                    if not VirtualCamera.is_enabled():
                        VirtualCamera.set_enabled(True)
                    vcam_dev = VirtualCamera.ensure_ready(
                        card_label=camera.name,
                        camera_id=camera.id,
                    )
                    if vcam_dev:
                        camera.extra["vcam_device"] = vcam_dev
                        log.info("Pre-allocated vcam %s for gphoto2 camera %s", vcam_dev, camera.name)

                if not backend.start_streaming(camera):
                    self.emit("error", _("Failed to start camera streaming process."))
                    return False

        # Resolve GStreamer source in background (pw-dump can take seconds)
        def _resolve_source() -> str:
            return self._manager.get_gst_source(
                camera, fmt, prefer_v4l2=self._prefer_v4l2,
            ) or ""

        def _on_source_resolved(gst_source: str) -> None:
            # Guard: camera may have changed while resolving
            if self._current_camera is not camera:
                return
            if not gst_source:
                self.emit("error", _("Failed to obtain GStreamer source for this camera."))
                return

            target_fps = 0
            if fmt and fmt.fps:
                target_fps = int(max(fmt.fps))

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
        """Direct camera sources — use tee + gtk4paintablesink (recording-ready).

        Virtual camera output uses a separate appsrc → v4l2sink pipeline fed
        from the probe callback, ensuring OpenCV effects are applied.

        When prefer_v4l2 is active, use appsink for flicker-free rendering
        (bypasses GStreamer's internal queue/timing, like guvcview).
        """
        # Radical anti-flicker: use appsink (like guvcview) instead of paintable sink
        if self._prefer_v4l2 and "v4l2src" in gst_source:
            if self._build_direct_pipeline(gst_source, target_fps):
                return True

        rate_limiter = ""
        if target_fps > 0:
            rate_limiter = f"videorate drop-only=true ! video/x-raw,framerate={target_fps}/1 ! "

        n_threads = min(os.cpu_count() or 2, 4)
        suffix = (
            f"queue max-size-buffers=2 leaky=downstream silent=true ! "
            f"{rate_limiter}"
            f"videoconvert n-threads={n_threads} name=conv ! "
            f"video/x-raw,format=BGRA ! "
            f"tee name=t ! "
            f"queue max-size-buffers=1 leaky=downstream silent=true ! "
            f"gtk4paintablesink sync=true"
        )

        base_pipeline = f"{gst_source} ! {suffix}"

        if self._try_start_paintable(base_pipeline):
            # Apply anti-flicker defaults (e.g. power_line_frequency) in background
            self._apply_anti_flicker_async()
            # Resolve vcam device in background to avoid blocking the UI
            self._resolve_vcam_async()
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
                    self._apply_anti_flicker_async()
                    self._resolve_vcam_async()
                    return True

        # All pipelines failed — check if device is busy (in background)
        if camera and camera.device_path:
            self._check_device_busy_async(camera.device_path)
            return False

        self.emit("error", _("Failed to start camera stream."))
        return False

    def _try_start_paintable(self, pipeline_str: str) -> bool:
        """Try to parse and start a paintable pipeline. Returns True on success."""
        log.info("Pipeline (paintable): %s", pipeline_str)
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
        self.emit("state-changed", "playing")

        return True

    def _build_direct_pipeline(self, gst_source: str, target_fps: int = 0) -> bool:
        """OpenCV V4L2 direct capture — flicker-free like guvcview.

        Bypasses GStreamer entirely. A background thread captures frames via
        cv2.VideoCapture (V4L2 mmap + libjpeg-turbo), stores the latest in
        _cv_latest_frame. A GLib timer on the main thread picks up new frames
        and renders them as GdkMemoryTexture — never blocks the UI.
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
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter.fourcc('M', 'J', 'P', 'G'))
        fmt = self._current_fmt
        if fmt:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, fmt.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, fmt.height)
            if fmt.fps:
                cap.set(cv2.CAP_PROP_FPS, max(fmt.fps))
        else:
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

        # Background capture thread — reads frames as fast as the camera
        # delivers them (V4L2 mmap blocking read) and stores the latest.
        self._cv_thread = threading.Thread(
            target=self._cv_capture_loop, daemon=True, name="bigcam-cv-capture"
        )
        self._cv_thread.start()

        # Main-thread timer picks up new frames and renders them (non-blocking).
        interval = 33 if target_fps <= 0 else max(16, 1000 // target_fps)
        self._cv_timer_id = GLib.timeout_add(interval, self._cv_render_frame)

        self._start_fps_counter()
        self._apply_anti_flicker_async()
        self._resolve_vcam_async()
        self.emit("state-changed", "playing")
        log.info("Direct OpenCV V4L2 preview started (no GStreamer)")
        return True

    def _cv_capture_loop(self) -> None:
        """Background thread: read frames from V4L2 as fast as they arrive."""
        cap = self._cv_cap
        stop = self._cv_stop_event
        # Suppress libjpeg-turbo "Corrupt JPEG data" stderr warnings
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        orig_stderr_fd = os.dup(2)
        try:
            while cap is not None and cap.isOpened() and not stop.is_set():
                os.dup2(devnull_fd, 2)
                ret, frame = cap.read()
                os.dup2(orig_stderr_fd, 2)
                if ret:
                    self._cv_latest_frame = frame
                    self._cv_frame_seq += 1
                elif stop.is_set():
                    break
        finally:
            os.dup2(orig_stderr_fd, 2)
            os.close(devnull_fd)
            os.close(orig_stderr_fd)

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
        """UDP/MPEG-TS sources (gphoto2, IP) — use appsink with manual texture rendering.

        Starts with a delay to let ffmpeg produce frames, then retries if needed.
        """
        log.debug(f"_build_appsink_pipeline: source={gst_source}")
        self._appsink_source = gst_source
        self._appsink_retry_count = 0
        self._appsink_max_retries = 30  # 30 * 500ms = 15s max wait (like old app)
        self._appsink_timer_id: int | None = None

        # BigCam is the sole writer to v4l2loopback so that OpenCV effects
        # are always visible on the virtual camera output.  For gPhoto2,
        # the device was pre-allocated in window.py; for IP cameras, we
        # allocate one here.
        pre_allocated = self._current_camera and self._current_camera.extra.get("vcam_device")
        if pre_allocated:
            cam_path = self._current_camera.device_path if self._current_camera else ""
            if pre_allocated != cam_path:
                log.info("Using pre-allocated vcam device %s for effects output", pre_allocated)
                self._start_vcam(pre_allocated)
        else:
            loopback_device = VirtualCamera.ensure_ready(
                card_label=self._current_camera.name if self._current_camera else None,
                camera_id=self._current_camera.id if self._current_camera else "",
            )
            cam_path = self._current_camera.device_path if self._current_camera else ""
            if loopback_device and loopback_device != cam_path:
                self._start_vcam(loopback_device)

        # Wait 2s for ffmpeg to start producing frames, then try
        self._appsink_timer_id = GLib.timeout_add(2000, self._try_appsink_first)
        return True

    def _try_appsink_first(self) -> bool:
        """First attempt after initial 2s delay, then switch to 500ms retries."""
        log.debug("_try_appsink_first called")
        self._appsink_timer_id = None
        if self._try_appsink_pipeline():
            # Need to retry — schedule at 500ms intervals
            log.debug("First attempt failed, scheduling 500ms retries")
            self._appsink_timer_id = GLib.timeout_add(500, self._try_appsink_pipeline)
        else:
            log.debug("First attempt: done (success or gave up)")
        return False  # don't repeat the 2s timer

    def _try_appsink_pipeline(self) -> bool:
        """Attempt to start the appsink pipeline, retry on failure.

        Uses dual pipeline strategy from the old working app:
        Pipeline 1: with address=127.0.0.1 (explicit localhost)
        Pipeline 2: without address (bind to 0.0.0.0)
        """
        # Check if we were stopped while waiting
        if self._current_camera is None:
            self._appsink_timer_id = None
            return False

        self._appsink_retry_count += 1
        gst_source = self._appsink_source
        log.debug(
            f"_try_appsink_pipeline: attempt {self._appsink_retry_count}/{self._appsink_max_retries}"
        )

        # Two pipeline variants, exactly as the old working app
        pipeline_attempts = [
            # Pipeline 1: explicit localhost bind
            (
                f"{gst_source} ! "
                f"video/x-raw,format=BGRA ! "
                f"tee name=t ! "
                f"queue max-size-buffers=2 leaky=downstream silent=true ! "
                f"appsink name=sink emit-signals=True drop=True max-buffers=2 sync=False"
            ),
            # Pipeline 2: fallback without address (bind all interfaces)
            (
                f"{gst_source.replace('address=127.0.0.1 ', '')} ! "
                f"video/x-raw,format=BGRA ! "
                f"tee name=t ! "
                f"queue max-size-buffers=2 leaky=downstream silent=true ! "
                f"appsink name=sink emit-signals=True drop=True max-buffers=2 sync=False"
            ),
        ]

        for i, pipeline_str in enumerate(pipeline_attempts):
            log.debug(f"Trying pipeline {i + 1}: {pipeline_str[:80]}...")
            try:
                pipeline = Gst.parse_launch(pipeline_str)
            except GLib.Error as e:
                log.debug(f"Pipeline {i + 1} parse error: {e}")
                continue

            if not isinstance(pipeline, Gst.Pipeline):
                pipe = Gst.Pipeline.new("bigcam")
                pipe.add(pipeline)
                pipeline = pipe

            appsink = pipeline.get_by_name("sink")
            if appsink is None:
                log.debug(f"Pipeline {i + 1}: no appsink found")
                pipeline.set_state(Gst.State.NULL)
                continue
            appsink.connect("new-sample", self._on_appsink_sample)

            bus = pipeline.get_bus()
            bus.add_signal_watch()

            ret = pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                log.debug(f"Pipeline {i + 1}: PLAYING failed immediately")
                pipeline.set_state(Gst.State.NULL)
                continue

            # Non-blocking state check — accept ASYNC as success
            ret, state, _ = pipeline.get_state(50 * Gst.MSECOND)
            log.debug(f"Pipeline {i + 1}: ret={ret}, state={state}")
            if ret == Gst.StateChangeReturn.FAILURE:
                pipeline.set_state(Gst.State.NULL)
                continue

            if state == Gst.State.PLAYING or ret in (
                Gst.StateChangeReturn.SUCCESS,
                Gst.StateChangeReturn.ASYNC,
            ):
                # Pipeline connected!
                log.debug(f"Pipeline {i + 1}: SUCCESS! Connected.")
                self._pipeline = pipeline
                self._bus_watch_id = bus.connect("message", self._on_bus_message)
                # Install FPS probe on appsink
                sink_pad = appsink.get_static_pad("sink")
                if sink_pad:
                    self._probe_id = sink_pad.add_probe(Gst.PadProbeType.BUFFER, self._on_frame_probe)
                    self._probe_pad = sink_pad
                self._start_fps_counter()
                self.emit("state-changed", "playing")
                self._appsink_timer_id = None
                return False  # stop retrying

            pipeline.set_state(Gst.State.NULL)

        # All pipelines failed this round
        if self._appsink_retry_count < self._appsink_max_retries:
            return True  # retry in 500ms
        self.emit("error", _("Failed to start camera stream."))
        self._appsink_timer_id = None
        return False

    def _start_pipeline(self) -> bool:
        bus = self._pipeline.get_bus()
        bus.add_signal_watch()
        self._bus_watch_id = bus.connect("message", self._on_bus_message)

        ret = self._pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            self.emit("error", _("Failed to start camera stream."))
            self.stop()
            return False

        self.emit("state-changed", "playing")
        return True

    def stop(self, stop_backend: bool = True, keep_vcam: bool = False) -> None:
        camera = self._current_camera
        self._stop_fps_counter()

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

        # Disconnect phone camera callback
        if self._phone_server_ref is not None:
            self._phone_server_ref.set_frame_callback(None)
            self._phone_server_ref = None
            self._phone_frame_pending = False
            self._stop_phone_v4l2()
            self._phone_v4l2_device = ""
            self._current_camera = None
            self._current_fmt = None
            self.emit("state-changed", "stopped")

        # Release retained frame data to free memory
        self._last_probe_bgr = None
        self._last_texture = None
        self._vcam_latest_frame = None
        self._vcam_pending_frame = None
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

        # Stop main GStreamer pipeline FIRST — releases the device/UDP port
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
        # OpenCV direct capture mode (no GStreamer pipeline)
        if self._cv_cap is not None and self._cv_cap.isOpened():
            return True
        if self._pipeline is None:
            return False
        _, state, _ = self._pipeline.get_state(0)
        return state == Gst.State.PLAYING

    # -- appsink rendering ---------------------------------------------------

    _appsink_sample_count = 0

    def _on_appsink_sample(self, appsink: Any) -> Gst.FlowReturn:
        sample = appsink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        caps = sample.get_caps()
        if not buf or not caps:
            return Gst.FlowReturn.OK
        s = caps.get_structure(0)
        w = s.get_value("width")
        h = s.get_value("height")
        result, map_info = buf.map(Gst.MapFlags.READ)
        if result:
            self._appsink_sample_count += 1
            if self._appsink_sample_count <= 3 or self._appsink_sample_count % 30 == 0:
                log.debug(f"appsink sample #{self._appsink_sample_count}: {w}x{h}")
            data = bytes(map_info.data)
            buf.unmap(map_info)
            # Store BGR frame for tools (QR, smile detection)
            try:
                bgra = np.frombuffer(data, dtype=np.uint8).reshape((h, w, 4))
                bgr = bgra[:, :, :3].copy()
                bgr = self._apply_frame_processing(bgr)
                self._distribute_processed_frame(bgr, w, h)
            except Exception:
                pass
            # Reconstruct BGRA from processed BGR for preview
            if self._has_processing_work() and self._last_probe_bgr is not None:
                display_bgr = cv2.flip(self._last_probe_bgr, 1) if self._mirror else self._last_probe_bgr
                bgra_out = cv2.cvtColor(display_bgr, cv2.COLOR_BGR2BGRA)
                data = bgra_out.tobytes()
            stride = len(data) // h
            glib_bytes = GLib.Bytes.new(data)
            GLib.idle_add(self._update_texture, w, h, stride, glib_bytes)
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

    def _resolve_vcam_async(self) -> None:
        """Resolve the virtual camera device in a background thread,
        then start the vcam pipeline on the main thread."""
        camera = self._current_camera
        if not camera:
            return

        # Phone cameras (AirPlay/scrcpy) already occupy a v4l2loopback
        # device as their source.  Use a separate allocation id so the
        # BigCam Virtual output goes to a *different* device.
        alloc_id = camera.id
        if camera.id.startswith("phone:"):
            alloc_id = f"vcam:{camera.id}"

        def _worker() -> str:
            device = VirtualCamera.ensure_ready(
                card_label=camera.name,
                camera_id=alloc_id,
            )
            # Prevent feedback loop: if the vcam device is the same device
            # we're reading from, skip vcam.
            if device and camera.device_path and device == camera.device_path:
                log.warning(
                    "vcam device %s is same as camera source — skipping to "
                    "avoid feedback loop",
                    device,
                )
                VirtualCamera.release_device(alloc_id)
                return ""
            return device

        def _on_done(device: str) -> None:
            if device and self._current_camera is camera and self.is_playing():
                self._vcam_alloc_id = alloc_id
                self._start_vcam(device)

        threading.Thread(
            target=lambda: GLib.idle_add(_on_done, _worker()),
            daemon=True,
        ).start()

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

    # -- virtual camera output (appsrc → v4l2sink) --------------------------

    def _start_vcam(self, device: str) -> None:
        """Prepare appsrc → v4l2sink pipeline for virtual camera output.

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
            f"appsrc name=src emit-signals=false is-live=true format=time block=false max-bytes={max_bytes} "
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
            log.error("vcam pipeline failed to start on %s — cleaning up", device)
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
                GLib.idle_add(self._rebuild_vcam_idle, w, h)
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
            log.warning("vcam push-buffer returned %s — stopping vcam", ret)
            self._stop_vcam()
            self._release_vcam_device()
            self._vcam_device = ""

    def _promote_vcam_to_background(self, camera: CameraInfo) -> None:
        """Keep the virtual camera alive when switching away from this camera.

        For V4L2 cameras: creates an OpenCV background feeder (or GStreamer
        v4l2src fallback) that reads from the physical camera.

        For gPhoto2 cameras: creates a GStreamer pipeline that reads from
        the UDP stream (ffmpeg keeps running) and writes to v4l2loopback.
        """
        device = self._vcam_device
        cam_id = camera.id
        self._stop_vcam()
        self._release_vcam_device()
        self._vcam_device = ""

        if not device:
            log.debug("promote_vcam_to_background: no vcam device for %s", cam_id)
            return

        # gPhoto2 cameras: ffmpeg streams via UDP, create a receiver pipeline
        # that reads the UDP stream and writes to v4l2loopback.
        if camera.backend == BackendType.GPHOTO2:
            udp_port = camera.extra.get("udp_port", 5000)
            self._stop_bg_vcam(cam_id)
            nthreads = min(os.cpu_count() or 2, 4)
            pipeline_str = (
                f"udpsrc port={udp_port} "
                "! tsdemux "
                "! decodebin "
                f"! videoconvert n-threads={nthreads} "
                "! video/x-raw,format=YUY2 "
                f"! v4l2sink device={device} sync=false"
            )
            log.info("Creating background vcam for gphoto2 %s: UDP:%s → %s",
                     camera.name, udp_port, device)
            try:
                pipe = Gst.parse_launch(pipeline_str)
                ret = pipe.set_state(Gst.State.PLAYING)
                if ret == Gst.StateChangeReturn.FAILURE:
                    log.warning("Background vcam (gphoto2) failed for %s", cam_id)
                    pipe.set_state(Gst.State.NULL)
                else:
                    self._bg_vcam_pipelines[cam_id] = pipe
                    log.info("Background vcam (gphoto2) active for %s on %s", camera.name, device)
            except GLib.Error as e:
                log.error("Failed to create gphoto2 background vcam: %s", e)
            return

        if camera.backend != BackendType.V4L2 or not camera.device_path:
            log.debug("promote_vcam_to_background: skipping non-v4l2 camera %s", cam_id)
            return

        # Defer the actual pipeline creation to allow the kernel to fully
        # release the device after OpenCV cap.release() / GStreamer NULL.
        GLib.timeout_add(
            250,
            self._create_bg_vcam_pipeline,
            cam_id,
            camera,
            device,
        )

    def _create_bg_vcam_pipeline(
        self, cam_id: str, camera: CameraInfo, device: str,
    ) -> bool:
        """Create a background virtual camera pipeline (deferred).

        Uses OpenCV V4L2 feeder when prefer_v4l2 is active (more reliable
        for USB cameras), otherwise falls back to GStreamer v4l2src pipeline.

        Returns False so GLib.timeout_add runs it only once.
        """
        # Guard: if this camera became the active one again (user switched
        # back quickly), don't create a background pipeline — the active
        # effects-aware vcam will handle it.
        if self._current_camera is camera:
            log.debug("_create_bg_vcam: camera %s is active again, skipping", cam_id)
            return False

        # Stop any existing background pipeline/feeder for this camera
        self._stop_bg_vcam(cam_id)

        # Prefer OpenCV V4L2 feeder for reliable USB camera capture
        if self._prefer_v4l2 and _HAS_CV2 and camera.device_path:
            feeder = _BgVcamFeeder(camera.device_path, device, camera.name)
            if feeder.start():
                self._bg_vcam_feeders[cam_id] = feeder
                return False
            log.warning("OpenCV bg vcam failed for %s, trying GStreamer", cam_id)

        # Fallback: GStreamer v4l2src → v4l2sink

        # Build a proper source with format caps using the backend
        # _v4l2_gst_source() already includes jpegdec for MJPEG formats
        backend = self._manager.get_backend(camera.backend)
        fmt_obj = None
        if backend and hasattr(backend, "_pick_best_format") and camera.formats:
            fmt_obj = backend._pick_best_format(camera)
        if backend and hasattr(backend, "_v4l2_gst_source"):
            source = backend._v4l2_gst_source(camera.device_path, camera, fmt_obj)
        else:
            source = f"v4l2src device={camera.device_path}"

        nthreads = min(os.cpu_count() or 2, 4)
        pipeline_str = (
            f"{source} ! "
            f"videoconvert n-threads={nthreads} ! "
            f"video/x-raw,format=YUY2 ! "
            f"v4l2sink device={device} sync=false"
        )
        log.info("Creating background vcam for %s: %s → %s", camera.name, camera.device_path, device)
        try:
            pipe = Gst.parse_launch(pipeline_str)
            ret = pipe.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                log.warning("Background vcam failed to start for %s", cam_id)
                pipe.set_state(Gst.State.NULL)
                return False
            self._bg_vcam_pipelines[cam_id] = pipe
            log.info("Background virtual camera active for %s on %s", camera.name, device)
        except GLib.Error as e:
            log.error("Failed to create background vcam: %s", e)
        return False

    def _stop_bg_vcam(self, camera_id: str) -> None:
        """Stop a specific background virtual camera pipeline or feeder."""
        pipe = self._bg_vcam_pipelines.pop(camera_id, None)
        if pipe:
            log.info("Stopping background vcam pipeline for %s", camera_id)
            pipe.set_state(Gst.State.NULL)
        feeder = self._bg_vcam_feeders.pop(camera_id, None)
        if feeder:
            feeder.stop()

    def stop_all_bg_vcams(self) -> None:
        """Stop all background virtual camera pipelines (used on app close)."""
        for cam_id in list(self._bg_vcam_pipelines):
            pipe = self._bg_vcam_pipelines.pop(cam_id, None)
            if pipe:
                pipe.set_state(Gst.State.NULL)
        self._bg_vcam_pipelines.clear()
        for cam_id in list(self._bg_vcam_feeders):
            feeder = self._bg_vcam_feeders.pop(cam_id, None)
            if feeder:
                feeder.stop()
        self._bg_vcam_feeders.clear()

    def ensure_bg_vcam(self, camera: CameraInfo) -> None:
        """Ensure a background vcam feeder exists for the given camera.

        Called at detection time for each V4L2 camera. Skips cameras that
        are already feeding or are the current active camera.
        """
        if not VirtualCamera.is_enabled():
            return
        if camera.backend != BackendType.V4L2 or not camera.device_path:
            return
        # Skip phone cameras — their v4l2 device is an output sink, not a source
        if camera.id.startswith("phone:"):
            return
        # Skip if this camera is already the active one (effects pipeline handles vcam)
        if self._current_camera and self._current_camera.id == camera.id:
            return
        # Skip if already has a background feeder or pipeline
        if camera.id in self._bg_vcam_feeders or camera.id in self._bg_vcam_pipelines:
            return
        # Allocate a v4l2loopback device
        device = VirtualCamera.ensure_ready(
            card_label=camera.name, camera_id=camera.id,
        )
        if not device:
            log.debug("ensure_bg_vcam: no loopback device for %s", camera.name)
            return
        # Start the background feeder
        if _HAS_CV2:
            feeder = _BgVcamFeeder(camera.device_path, device, camera.name)
            if feeder.start():
                self._bg_vcam_feeders[camera.id] = feeder
                log.info("Background vcam started at detection for %s on %s", camera.name, device)
                return
            log.warning("OpenCV bg vcam failed at detection for %s", camera.name)
        # Fallback: GStreamer v4l2src pipeline
        self._create_bg_vcam_pipeline(camera.id, camera, device)

    # -- phone camera --------------------------------------------------------

    _phone_server_ref: Any = None
    _phone_frame_pending: bool = False
    _phone_v4l2_pipeline: Any = None  # GStreamer appsrc → v4l2sink for virtual cam
    _phone_v4l2_appsrc: Any = None
    _phone_v4l2_caps_set: bool = False
    _phone_v4l2_device: str = ""
    _phone_v4l2_w: int = 0
    _phone_v4l2_h: int = 0
    _phone_v4l2_building: bool = False
    _phone_v4l2_pending_frame: Any = None  # (bgr_ndarray, w, h) or None

    def _start_phone_camera(self, camera: CameraInfo) -> bool:
        """Receive frames from the phone camera WebSocket server."""
        server = camera.extra.get("phone_server")
        if not server:
            self.emit("error", _("Phone camera server not available."))
            return False
        self._use_appsink = True  # use texture-based rendering
        self._phone_server_ref = server
        server.set_frame_callback(self._on_phone_frame)

        # Start v4l2loopback output if virtual camera is enabled
        loopback_device = VirtualCamera.ensure_ready(
            card_label=camera.name if camera else None,
            camera_id=camera.id if camera else "",
        )
        if loopback_device:
            self._start_phone_v4l2(loopback_device)

        self._start_fps_counter()
        self.emit("state-changed", "playing")
        log.info("Phone camera started — waiting for frames")
        return True

    def _start_phone_v4l2(self, device: str) -> None:
        """Create appsrc → videoconvert → v4l2sink pipeline for phone → virtual camera."""
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
            "appsrc name=src emit-signals=false is-live=true format=time "
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
            log.info("Phone v4l2: resizing frame %dx%d → %dx%d", w, h, pw, ph)
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

    def _on_phone_frame(self, bgr: Any) -> None:
        """Handle a BGR frame from the phone WebSocket (asyncio thread)."""
        if self._current_camera is None:
            return
        # Drop frame if GTK hasn't consumed the previous one
        if self._phone_frame_pending:
            return
        self._phone_frame_pending = True

        h, w = bgr.shape[:2]

        # Apply effects (mirror is handled via CSS on preview, not on data)
        if self._effects.has_active_effects():
            bgr = self._effects.apply(bgr)

        # Store for snapshot/tools — mirror for photo/recording
        self._last_probe_bgr = cv2.flip(bgr, 1) if self._mirror else bgr

        # Write to video recorder if active (with mirror for consistency with preview)
        rec = self._video_recorder
        if rec and rec.is_recording:
            rec.write_frame(self._last_probe_bgr)

        # Feed virtual camera via appsrc if active
        if self._phone_v4l2_device:
            self._push_phone_v4l2(bgr, w, h)

        # BGR → BGRA using OpenCV SIMD (much faster than numpy manual copy)
        bgra = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
        data = bgra.tobytes()

        stride = w * 4
        glib_bytes = GLib.Bytes.new(data)
        GLib.idle_add(self._update_phone_texture, w, h, stride, glib_bytes)

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
                    "cannot open", "ocupado",
                )
            )
            if busy and dev_path:
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
            f"queue max-size-buffers=2 leaky=downstream silent=true ! "
            f"videoconvert n-threads={n_threads} name=conv ! "
            f"video/x-raw,format=BGRA ! "
            f"tee name=t ! "
            f"queue max-size-buffers=1 leaky=downstream silent=true ! "
            f"gtk4paintablesink sync=true"
        )
        fallback_pipeline = f"{v4l2_source} ! {suffix}"
        if self._try_start_paintable(fallback_pipeline):
            self._current_camera = camera
            if loopback_device:
                self._start_vcam(loopback_device)
            return True
        return False
