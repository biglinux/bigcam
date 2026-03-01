"""Stream Engine – GStreamer pipeline lifecycle for camera preview."""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gst, GstVideo, Gdk, GLib, GObject

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
    """Return list of process names currently using a V4L2 device."""
    try:
        result = subprocess.run(
            ["fuser", device_path],
            capture_output=True, text=True, timeout=3,
        )
        pids = result.stdout.strip().split()
        names: list[str] = []
        for pid in pids:
            pid = pid.strip().rstrip("m")
            if not pid.isdigit():
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


class StreamEngine(GObject.Object):
    """Builds and manages the GStreamer preview pipeline for any camera backend."""

    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "error": (GObject.SignalFlags.RUN_LAST, None, (str,)),
        "new-texture": (GObject.SignalFlags.RUN_LAST, None, (object,)),
    }

    def __init__(self, camera_manager: CameraManager) -> None:
        super().__init__()
        self._manager = camera_manager
        self._pipeline: Gst.Pipeline | None = None
        self._bus_watch_id: int | None = None
        self._current_camera: CameraInfo | None = None
        self._gtksink: Any = None
        self._use_appsink = False
        self._last_texture: Gdk.Texture | None = None
        self._frame_count: int = 0
        self._current_fps: float = 0.0
        self._fps_timer_id: int | None = None
        self._mirror: bool = False
        self._effects = EffectPipeline()
        self._probe_debug_count: int = 0
        self._last_probe_bgr = None
        self._overlay_rects: list[tuple] = []  # [(x,y,w,h), ...] for QR overlay
        self._video_recorder: Any = None  # set by window to enable phone recording

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

    def _on_frame_probe(self, pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        self._frame_count += 1
        return Gst.PadProbeReturn.OK

    def _on_paintable_probe(self, pad: Gst.Pad, info: Gst.PadProbeInfo) -> Gst.PadProbeReturn:
        """Buffer probe on tee sink — applies OpenCV effects via buffer replacement."""
        self._frame_count += 1
        buf = info.get_buffer()
        if buf is None:
            return Gst.PadProbeReturn.OK
        caps = pad.get_current_caps()
        if caps is None:
            return Gst.PadProbeReturn.OK
        s = caps.get_structure(0)
        w = s.get_value("width")
        h = s.get_value("height")
        fmt = s.get_string("format")
        self._probe_debug_count += 1
        if self._probe_debug_count <= 3:
            print(f"[DEBUG] paintable_probe: fmt={fmt}, {w}x{h}")

        ok, map_info = buf.map(Gst.MapFlags.READ)
        if not ok:
            return Gst.PadProbeReturn.OK
        bgr = None
        result = None
        try:
            import numpy as np
            import cv2
            raw = bytes(map_info.data)
            if fmt in ("BGRA", "BGRx"):
                frame = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 4))
                bgr = frame[:, :, :3].copy()
            elif fmt == "BGR":
                bgr = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 3)).copy()
            elif fmt == "RGB":
                rgb = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 3)).copy()
                bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            elif fmt == "I420":
                yuv = np.frombuffer(raw, dtype=np.uint8).reshape((h * 3 // 2, w))
                bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_I420)
            elif fmt == "NV12":
                yuv = np.frombuffer(raw, dtype=np.uint8).reshape((h * 3 // 2, w))
                bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_NV12)
            elif fmt in ("YUY2", "YUYV"):
                yuv = np.frombuffer(raw, dtype=np.uint8).reshape((h, w * 2))
                bgr = cv2.cvtColor(yuv, cv2.COLOR_YUV2BGR_YUY2)
            if bgr is not None:
                needs_replace = self._effects.has_active_effects() or self._overlay_rects
                if needs_replace:
                    processed = bgr.copy()
                    if self._effects.has_active_effects():
                        processed = self._effects.apply(processed)
                    if self._overlay_rects:
                        for rect in self._overlay_rects:
                            x, y, rw, rh = rect
                            cv2.rectangle(processed, (x, y), (x + rw, y + rh),
                                          (0, 255, 0), 3)
                    # Store for snapshot/photo (mirror applied for consistency with preview)
                    self._last_probe_bgr = cv2.flip(processed, 1) if self._mirror else processed
                    # Convert back to original format
                    if fmt in ("BGRA", "BGRx"):
                        out = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 4)).copy()
                        out[:, :, :3] = processed
                        result = out.tobytes()
                    elif fmt == "BGR":
                        result = processed.tobytes()
                    elif fmt == "RGB":
                        result = cv2.cvtColor(processed, cv2.COLOR_BGR2RGB).tobytes()
                    elif fmt == "I420":
                        result = cv2.cvtColor(processed, cv2.COLOR_BGR2YUV_I420).tobytes()
                    elif fmt == "NV12":
                        result = cv2.cvtColor(processed, cv2.COLOR_BGR2YUV_I420).tobytes()
                    elif fmt in ("YUY2", "YUYV"):
                        if hasattr(cv2, 'COLOR_BGR2YUV_YUY2'):
                            result = cv2.cvtColor(processed, cv2.COLOR_BGR2YUV_YUY2).tobytes()
                else:
                    self._last_probe_bgr = cv2.flip(bgr, 1) if self._mirror else bgr
        except Exception as e:
            if self._probe_debug_count <= 5:
                print(f"[DEBUG] paintable_probe error: {e}")
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

    def capture_snapshot(self, output_path: str) -> bool:
        """Save the current preview frame as a PNG file.

        Works for both paintable and appsink pipelines.
        """
        # Appsink pipeline stores last texture directly
        if self._use_appsink and self._last_texture:
            try:
                self._last_texture.save_to_png(output_path)
                return True
            except Exception as exc:
                log.error("Failed to save appsink snapshot: %s", exc)
                return False

        # Paintable pipeline — capture from probe's last frame
        if self._last_probe_bgr is not None:
            try:
                import cv2
                cv2.imwrite(output_path, self._last_probe_bgr)
                return True
            except Exception as exc:
                log.error("Failed to save probe snapshot: %s", exc)
                return False

        # Fallback — try paintable directly
        if self._gtksink:
            paintable = self._gtksink.get_property("paintable")
            if paintable and hasattr(paintable, "save_to_png"):
                try:
                    paintable.save_to_png(output_path)
                    return True
                except Exception:
                    pass
        return False

    def play(self, camera: CameraInfo, fmt: VideoFormat | None = None,
             streaming_ready: bool = False) -> bool:
        """Build and start the pipeline for *camera*.

        Args:
            streaming_ready: If True, skip start_streaming() because caller
                             already handled it (e.g. window async setup).
        """
        self.stop()
        self._current_camera = camera
        self._use_appsink = camera.backend in _APPSINK_BACKENDS
        print(f"[DEBUG] play: camera={camera.name}, backend={camera.backend}, use_appsink={self._use_appsink}, streaming_ready={streaming_ready}")

        # Phone camera – frames come via WebRTC, no GStreamer pipeline needed
        if camera.backend == BackendType.PHONE:
            return self._start_phone_camera(camera)

        # Some backends need an external streaming process first
        if not streaming_ready:
            backend = self._manager.get_backend(camera.backend)
            if backend and hasattr(backend, "needs_streaming_setup") and backend.needs_streaming_setup():
                if not backend.start_streaming(camera):
                    self.emit("error", _("Failed to start camera streaming process."))
                    return False

        gst_source = self._manager.get_gst_source(camera, fmt)
        if not gst_source:
            self.emit("error", _("Failed to obtain GStreamer source for this camera."))
            return False

        if self._use_appsink:
            return self._build_appsink_pipeline(gst_source)
        return self._build_paintable_pipeline(gst_source)

    def _build_paintable_pipeline(self, gst_source: str) -> bool:
        """Direct camera sources — use tee + gtk4paintablesink (recording-ready).

        Automatically feeds the stream to a v4l2loopback virtual camera when
        available, allowing other applications to share the camera.
        """
        loopback_device = VirtualCamera.ensure_ready(
            card_label=self._current_camera.name if self._current_camera else None
        )

        base_pipeline = (
            f"{gst_source} ! "
            f"queue max-size-buffers=2 leaky=downstream silent=true ! "
            f"videoconvert n-threads=2 name=conv ! "
            f"video/x-raw,format=BGRA ! "
            f"tee name=t ! "
            f"queue max-size-buffers=2 leaky=downstream silent=true ! "
            f"gtk4paintablesink sync=false"
        )

        if loopback_device:
            loopback_pipeline = base_pipeline + (
                f" t. ! queue max-size-buffers=2 leaky=downstream silent=true ! "
                f"videoconvert ! video/x-raw,format=YUY2 ! "
                f"v4l2sink device={loopback_device} sync=false"
            )
            if self._try_start_paintable(loopback_pipeline):
                log.info("Virtual camera sharing active on %s", loopback_device)
                return True
            log.warning("Virtual camera output failed, starting without it")

        if self._try_start_paintable(base_pipeline):
            return True

        # All pipelines failed — check if device is busy
        if self._current_camera and self._current_camera.device_path:
            users = _find_device_users(self._current_camera.device_path)
            if users:
                apps = ", ".join(users)
                self.emit("error", _("Camera in use by: %s") % apps)
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
            probe_pad.add_probe(Gst.PadProbeType.BUFFER, self._on_paintable_probe)
        self._start_fps_counter()
        self.emit("state-changed", "playing")
        return True

    def _build_appsink_pipeline(self, gst_source: str) -> bool:
        """UDP/MPEG-TS sources (gphoto2, IP) — use appsink with manual texture rendering.

        Starts with a delay to let ffmpeg produce frames, then retries if needed.
        """
        print(f"[DEBUG] _build_appsink_pipeline: source={gst_source}")
        self._appsink_source = gst_source
        self._appsink_retry_count = 0
        self._appsink_max_retries = 30  # 30 * 500ms = 15s max wait (like old app)
        self._appsink_timer_id: int | None = None
        # Wait 2s for ffmpeg to start producing frames, then try
        self._appsink_timer_id = GLib.timeout_add(2000, self._try_appsink_first)
        return True

    def _try_appsink_first(self) -> bool:
        """First attempt after initial 2s delay, then switch to 500ms retries."""
        print("[DEBUG] _try_appsink_first called")
        self._appsink_timer_id = None
        if self._try_appsink_pipeline():
            # Need to retry — schedule at 500ms intervals
            print("[DEBUG] First attempt failed, scheduling 500ms retries")
            self._appsink_timer_id = GLib.timeout_add(500, self._try_appsink_pipeline)
        else:
            print("[DEBUG] First attempt: done (success or gave up)")
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
        print(f"[DEBUG] _try_appsink_pipeline: attempt {self._appsink_retry_count}/{self._appsink_max_retries}")

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
            print(f"[DEBUG] Trying pipeline {i+1}: {pipeline_str[:80]}...")
            try:
                pipeline = Gst.parse_launch(pipeline_str)
            except GLib.Error as e:
                print(f"[DEBUG] Pipeline {i+1} parse error: {e}")
                continue

            if not isinstance(pipeline, Gst.Pipeline):
                pipe = Gst.Pipeline.new("bigcam")
                pipe.add(pipeline)
                pipeline = pipe

            appsink = pipeline.get_by_name("sink")
            if appsink is None:
                print(f"[DEBUG] Pipeline {i+1}: no appsink found")
                pipeline.set_state(Gst.State.NULL)
                continue
            appsink.connect("new-sample", self._on_appsink_sample)

            bus = pipeline.get_bus()
            bus.add_signal_watch()

            ret = pipeline.set_state(Gst.State.PLAYING)
            if ret == Gst.StateChangeReturn.FAILURE:
                print(f"[DEBUG] Pipeline {i+1}: PLAYING failed immediately")
                pipeline.set_state(Gst.State.NULL)
                continue

            # Wait briefly to check state (max 2s)
            ret, state, _ = pipeline.get_state(2 * Gst.SECOND)
            print(f"[DEBUG] Pipeline {i+1}: ret={ret}, state={state}")
            if ret == Gst.StateChangeReturn.FAILURE:
                pipeline.set_state(Gst.State.NULL)
                continue

            if state == Gst.State.PLAYING or ret in (
                Gst.StateChangeReturn.SUCCESS,
                Gst.StateChangeReturn.ASYNC,
            ):
                # Pipeline connected!
                print(f"[DEBUG] Pipeline {i+1}: SUCCESS! Connected.")
                self._pipeline = pipeline
                self._bus_watch_id = bus.connect("message", self._on_bus_message)
                # Install FPS probe on appsink
                sink_pad = appsink.get_static_pad("sink")
                if sink_pad:
                    sink_pad.add_probe(Gst.PadProbeType.BUFFER, self._on_frame_probe)
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

    def stop(self, stop_backend: bool = True) -> None:
        camera = self._current_camera
        self._stop_fps_counter()

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
            self.emit("state-changed", "stopped")

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
            self.emit("state-changed", "stopped")

        if camera and stop_backend:
            backend = self._manager.get_backend(camera.backend)
            if backend and hasattr(backend, "stop_streaming"):
                backend.stop_streaming(camera)

    def is_playing(self) -> bool:
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
                print(f"[DEBUG] appsink sample #{self._appsink_sample_count}: {w}x{h}")
            data = bytes(map_info.data)
            buf.unmap(map_info)
            # Store BGR frame for tools (QR, smile detection)
            try:
                import numpy as np
                import cv2
                bgra = np.frombuffer(data, dtype=np.uint8).reshape((h, w, 4))
                bgr = bgra[:, :, :3].copy()
                if self._effects.has_active_effects():
                    bgr = self._effects.apply(bgr)
                # Mirror for snapshot/tools (flip when mirror ON)
                self._last_probe_bgr = cv2.flip(bgr, 1) if self._mirror else bgr
            except Exception:
                pass
            # Apply effects to BGRA data for preview texture
            if self._effects.has_active_effects():
                data = self._effects.apply_bgra(data, w, h)
            stride = len(data) // h
            glib_bytes = GLib.Bytes.new(data)
            GLib.idle_add(self._update_texture, w, h, stride, glib_bytes)
        return Gst.FlowReturn.OK

    def _update_texture(self, w: int, h: int, stride: int, glib_bytes: GLib.Bytes) -> bool:
        try:
            texture = Gdk.MemoryTexture.new(
                w, h, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED, glib_bytes, stride
            )
            self._last_texture = texture
            self.emit("new-texture", texture)
        except Exception:
            pass
        return False

    # -- phone camera --------------------------------------------------------

    _phone_server_ref: Any = None
    _phone_frame_pending: bool = False
    _phone_v4l2_pipeline: Any = None  # GStreamer appsrc → v4l2sink for virtual cam
    _phone_v4l2_appsrc: Any = None
    _phone_v4l2_caps_set: bool = False
    _phone_v4l2_device: str = ""
    _phone_v4l2_w: int = 0
    _phone_v4l2_h: int = 0

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
            card_label=camera.name if camera else None
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
        # Pipeline will be created on first frame when we know the resolution

    def _rebuild_phone_v4l2(self, w: int, h: int) -> None:
        """(Re)create the v4l2 pipeline with correct resolution."""
        self._stop_phone_v4l2()
        device = self._phone_v4l2_device
        if not device:
            log.warning("Cannot rebuild phone v4l2: no device set")
            return
        pipeline_str = (
            "appsrc name=src emit-signals=false is-live=true format=time "
            f"caps=video/x-raw,format=BGR,width={w},height={h},framerate=30/1 "
            "! videoconvert n-threads=2 "
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
        log.info("Phone virtual camera output started on %s (%dx%d) state=%s", device, w, h, ret)

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
        """
        if not self._phone_v4l2_device:
            return
        # First frame: create pipeline with the initial resolution
        if not self._phone_v4l2_caps_set:
            self._rebuild_phone_v4l2(w, h)
        appsrc = self._phone_v4l2_appsrc
        if not appsrc:
            return
        # Resize if resolution changed (rotation) to keep pipeline stable
        pw, ph = self._phone_v4l2_w, self._phone_v4l2_h
        if w != pw or h != ph:
            import cv2
            log.info("Phone v4l2: resizing frame %dx%d → %dx%d", w, h, pw, ph)
            bgr = cv2.resize(bgr, (pw, ph))
        data = bytes(bgr.data)
        expected = pw * ph * 3
        if len(data) != expected:
            log.warning("Phone v4l2: buffer size mismatch: got %d, expected %d", len(data), expected)
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

        import cv2
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

    def _update_phone_texture(self, w: int, h: int, stride: int, glib_bytes: GLib.Bytes) -> bool:
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

            busy = any(
                kw in (error_text + (dbg or "")).lower()
                for kw in ("resource busy", "busy", "ebusy", "cannot open")
            )
            if busy and self._current_camera and self._current_camera.device_path:
                users = _find_device_users(self._current_camera.device_path)
                if users:
                    apps = ", ".join(users)
                    error_text = _("Camera in use by: %s") % apps
                else:
                    error_text = _("Camera is being used by another application.")

            self.stop()
            self.emit("error", error_text)
        elif msg.type == Gst.MessageType.WARNING:
            err, dbg = msg.parse_warning()
            wmsg = err.message if err else ""
            # Suppress expected leaky queue warnings
            if "descartada" not in wmsg and "dropping" not in wmsg.lower():
                log.warning("GStreamer warning: %s", wmsg)
