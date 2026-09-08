"""Camera Manager – detects, tracks and switches between cameras from all backends."""

from __future__ import annotations

import logging
import re
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import glob

log = logging.getLogger(__name__)

from gi.repository import Gio, GLib, GObject

from constants import BackendType
from core.camera_backend import CameraBackend, CameraControl, CameraInfo, VideoFormat
from core.backends.v4l2_backend import V4L2Backend
from core.backends.gphoto2_backend import GPhoto2Backend
from core.backends.libcamera_backend import LibcameraBackend
from core.backends.pipewire_backend import PipeWireBackend
from core.backends.ip_backend import IPBackend
from core.camera_identity import MANUAL_BACKENDS, unique_cameras
from utils.async_worker import run_async
from utils.i18n import _


class CameraManager(GObject.Object):
    """Orchestrates camera detection across all backends with hotplug support."""

    __gsignals__ = {
        "cameras-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "camera-error": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }

    def __init__(self) -> None:
        super().__init__()
        self._backends: list[CameraBackend] = []
        self._cameras: list[CameraInfo] = []
        self._detecting = False
        self._detection_generation = 0
        self._detection_task = None
        self._pending_rescan = False
        self._closed = False
        self._first_detection = True
        self._hotplug_timer: int | None = None
        self._last_lsusb: str = ""
        self._last_video_devs: str = ""

        # Gio.FileMonitor for instant /dev/ changes
        self._dev_monitor: Gio.FileMonitor | None = None
        # Gio.FileMonitors for /dev/bus/usb/ directories (gphoto2 cameras)
        self._usb_bus_monitors: list[Gio.FileMonitor] = []
        # Debounce timer for batching rapid device events
        self._debounce_timer: int | None = None
        # Lock to protect shared polling state across threads
        self._poll_lock = threading.Lock()
        # Stop event for the polling thread
        self._poll_stop_event = threading.Event()
        self._poll_thread: threading.Thread | None = None

        self._register_backends()

    # -- backend registration ------------------------------------------------

    def _register_backends(self) -> None:
        candidates: list[CameraBackend] = [
            V4L2Backend(),
            GPhoto2Backend(),
            LibcameraBackend(),
            PipeWireBackend(),
            IPBackend(),
        ]
        for b in candidates:
            try:
                if b.is_available():
                    self._backends.append(b)
            except Exception:
                log.debug("Backend %s check failed", type(b).__name__, exc_info=True)

    @property
    def cameras(self) -> list[CameraInfo]:
        return list(self._cameras)

    @property
    def available_backends(self) -> list[BackendType]:
        return [b.get_backend_type() for b in self._backends]

    def get_backend(self, backend_type: BackendType) -> CameraBackend | None:
        for b in self._backends:
            if b.get_backend_type() == backend_type:
                return b
        return None

    # -- detection -----------------------------------------------------------

    def detect_cameras_async(self, force_emit: bool = False) -> None:
        """Detect in parallel, then publish one complete generation on the main loop.

        Partial snapshots used to remove cameras whose slower backend had not yet
        returned. A hotplug event during a scan now requests one coalesced rescan.
        """
        if self._closed:
            return
        if self._detecting:
            self._pending_rescan = True
            return
        self._detecting = True
        self._force_emit = force_emit
        self._detection_generation += 1
        generation = self._detection_generation
        backends = [b for b in self._backends if b.get_backend_type() != BackendType.IP]

        def detect_one(backend):
            try:
                return backend.detect_cameras()
            except Exception:
                log.exception("Camera detection failed for %s", type(backend).__name__)
                # A failed scan is not proof that the device disappeared.
                return [c for c in previous if c.backend == backend.get_backend_type()]

        previous = self.cameras
        def worker():
            if not backends:
                return []
            with ThreadPoolExecutor(max_workers=min(4, len(backends))) as pool:
                groups = list(pool.map(detect_one, backends))
            return unique_cameras([camera for group in groups for camera in group])

        def done(cameras):
            if self._closed or generation != self._detection_generation:
                return
            self._detecting = False
            self._on_detection_done(cameras)
            if self._pending_rescan:
                self._pending_rescan = False
                self.detect_cameras_async(force_emit=True)

        def failed(error):
            if generation == self._detection_generation and not self._closed:
                self._detecting = False
                self.emit("camera-error", _("Camera detection failed. Try refreshing the camera list."))
        self._detection_task = run_async(worker, on_success=done, on_error=failed)

    def _on_detection_done(self, cameras: list[CameraInfo]) -> bool:
        manual = [c for c in self._cameras if c.backend in MANUAL_BACKENDS]
        # Keep the live objects for existing sessions; refresh metadata in place.
        old = {c.id: c for c in self._cameras}
        merged = unique_cameras([*cameras, *manual])
        result = []
        for camera in merged:
            existing = old.get(camera.id)
            if existing is not None:
                existing.name = camera.name
                existing.formats = camera.formats or existing.formats
                existing.capabilities = camera.capabilities
                existing.extra.update(camera.extra)
                result.append(existing)
            else:
                result.append(camera)
        changed = self._first_detection or set(old) != {c.id for c in result} or self._force_emit
        self._cameras = result
        self._force_emit = False
        if changed:
            self._first_detection = False
            self.emit("cameras-changed")
        return GLib.SOURCE_REMOVE

    def add_ip_cameras(self, entries: list[dict[str, str]]) -> None:
        """Add manually-configured IP cameras."""
        backend = self.get_backend(BackendType.IP)
        if not isinstance(backend, IPBackend):
            return
        ip_cams = backend.cameras_from_urls(entries)
        # Remove old IP cameras
        self._cameras = [c for c in self._cameras if c.backend != BackendType.IP]
        self._cameras.extend(ip_cams)
        self.emit("cameras-changed")

    def add_phone_camera(self, camera: CameraInfo) -> None:
        """Register a phone camera source (WebRTC, scrcpy or AirPlay)."""
        self._cameras = [
            c for c in self._cameras if c.id != camera.id
        ]
        self._cameras.append(camera)
        self.emit("cameras-changed")

    def remove_phone_camera(self) -> None:
        """Remove phone camera from the list."""
        had = any(c.id.startswith("phone:") for c in self._cameras)
        self._cameras = [
            c for c in self._cameras if not c.id.startswith("phone:")
        ]
        if had:
            self.emit("cameras-changed")

    def remove_scrcpy_camera(self, device_id: str) -> None:
        """Remove a specific scrcpy android camera from the list."""
        target_id = f"scrcpy:{device_id}"
        had = any(c.id == target_id for c in self._cameras)
        self._cameras = [
            c for c in self._cameras if c.id != target_id
        ]
        if had:
            self.emit("cameras-changed")

    def remove_airplay_cameras(self) -> None:
        """Remove airplay iOS/macOS cameras from the list."""
        had = any(c.id.startswith("airplay:") for c in self._cameras)
        self._cameras = [
            c for c in self._cameras if not c.id.startswith("airplay:")
        ]
        if had:
            self.emit("cameras-changed")

    # -- controls proxy ------------------------------------------------------

    def get_controls(self, camera: CameraInfo) -> list[CameraControl]:
        if camera.backend == BackendType.PHONE:
            from core.camera_backend import CameraControl
            from constants import ControlCategory, ControlType
            vol = 100
            if "phone_server" in camera.extra:
                vol = int(camera.extra["phone_server"]._desired_volume * 100)
            return [
                CameraControl(
                    id="audio_volume",
                    name=_("Audio Volume"),
                    category=ControlCategory.ADVANCED,
                    control_type=ControlType.INTEGER,
                    value=vol,
                    default=100,
                    minimum=0,
                    maximum=100,
                )
            ]
        backend = self.get_backend(camera.backend)
        if backend:
            if hasattr(backend, "get_controls"):
                return backend.get_controls(camera)
        return []

    def set_control(self, camera: CameraInfo, control_id: str, value: Any) -> bool:
        if camera.backend == BackendType.PHONE:
            if control_id == "audio_volume" and "phone_server" in camera.extra:
                camera.extra["phone_server"].set_audio_volume(int(value) / 100.0)
                return True
            return False

        backend = self.get_backend(camera.backend)
        if backend:
            return backend.set_control(camera, control_id, value)
        return False

    def reset_all_controls(
        self, camera: CameraInfo, controls: list[CameraControl]
    ) -> None:
        backend = self.get_backend(camera.backend)
        if backend:
            backend.reset_all_controls(camera, controls)

    def apply_anti_flicker(self, camera: CameraInfo) -> None:
        backend = self.get_backend(camera.backend)
        if backend and hasattr(backend, "apply_anti_flicker"):
            backend.apply_anti_flicker(camera)

    # -- gstreamer proxy -----------------------------------------------------

    def get_gst_source(self, camera: CameraInfo, fmt: VideoFormat | None = None,
                       prefer_v4l2: bool = False) -> str:
        backend_type = camera.backend
        if backend_type in (BackendType.AIRPLAY, BackendType.SCRCPY):
            backend_type = BackendType.V4L2
        backend = self.get_backend(backend_type)
        if isinstance(backend, V4L2Backend):
            return backend.get_gst_source(camera, fmt, prefer_v4l2=prefer_v4l2)
        return backend.get_gst_source(camera, fmt) if backend else ""

    # -- photo proxy ---------------------------------------------------------

    def can_capture_photo(self, camera: CameraInfo) -> bool:
        backend = self.get_backend(camera.backend)
        return backend.can_capture_photo() if backend else False

    def capture_photo(self, camera: CameraInfo, output_path: str) -> bool:
        backend = self.get_backend(camera.backend)
        return backend.capture_photo(camera, output_path) if backend else False

    # -- hotplug detection ---------------------------------------------------

    def start_hotplug(self, interval_ms: int = 5000) -> None:
        """Start USB hotplug monitoring using /dev/ inotify + polling fallback."""
        # Take a baseline snapshot so the first poll doesn't false-trigger
        self._snapshot_device_state()
        log.info("Hotplug monitoring started (poll=%dms, baseline=%s)",
                 interval_ms, self._last_video_devs)

        # Start Gio.FileMonitor on /dev/ for instant V4L2 device detection
        if self._dev_monitor is None:
            try:
                dev_dir = Gio.File.new_for_path("/dev")
                self._dev_monitor = dev_dir.monitor_directory(
                    Gio.FileMonitorFlags.NONE, None
                )
                self._dev_monitor.connect("changed", self._on_dev_changed)
                log.info("Started /dev/ monitor for instant hotplug detection")
            except Exception:
                log.warning("Failed to start /dev/ file monitor", exc_info=True)

        # Monitor /dev/bus/usb/ directories for instant gphoto2 camera detection
        if not self._usb_bus_monitors:
            try:
                for bus_dir in sorted(glob.glob("/dev/bus/usb/*/")):
                    gf = Gio.File.new_for_path(bus_dir)
                    mon = gf.monitor_directory(Gio.FileMonitorFlags.NONE, None)
                    mon.connect("changed", self._on_usb_bus_changed)
                    self._usb_bus_monitors.append(mon)
                if self._usb_bus_monitors:
                    log.info("Started %d USB bus monitors for gphoto2 hotplug",
                             len(self._usb_bus_monitors))
            except Exception:
                log.warning("Failed to start USB bus monitors", exc_info=True)

        # Keep polling as a safety-net fallback (single persistent thread)
        if self._poll_thread is None or not self._poll_thread.is_alive():
            self._poll_stop_event.clear()
            self._poll_thread = threading.Thread(
                target=self._poll_hotplug_loop,
                args=(interval_ms / 1000.0,),
                daemon=True,
                name="bigcam-hotplug-poll",
            )
            self._poll_thread.start()

    def stop_hotplug(self) -> None:
        # Cancel pending debounce
        if self._debounce_timer is not None:
            GLib.source_remove(self._debounce_timer)
            self._debounce_timer = None

        if self._hotplug_timer is not None:
            GLib.source_remove(self._hotplug_timer)
            self._hotplug_timer = None

        # Stop the polling thread
        self._poll_stop_event.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=2.0)
            self._poll_thread = None

        if self._dev_monitor is not None:
            self._dev_monitor.cancel()
            self._dev_monitor = None

        for mon in self._usb_bus_monitors:
            mon.cancel()
        self._usb_bus_monitors.clear()

    def _snapshot_device_state(self) -> None:
        """Capture current USB + video device state as baseline (runs in background)."""
        def _do_snapshot() -> None:
            with self._poll_lock:
                try:
                    result = subprocess.run(
                        ["lsusb"], capture_output=True, text=True, timeout=5
                    )
                    self._last_lsusb = result.stdout
                except Exception:
                    self._last_lsusb = ""
                try:
                    self._last_video_devs = ",".join(
                        sorted(glob.glob("/dev/video*"))
                    )
                except Exception:
                    self._last_video_devs = ""

        threading.Thread(target=_do_snapshot, daemon=True).start()

    def _on_dev_changed(
        self,
        _monitor: Gio.FileMonitor,
        file: Gio.File,
        _other_file: Gio.File | None,
        event_type: Gio.FileMonitorEvent,
    ) -> None:
        """Instant callback when a file in /dev/ is created or deleted."""
        name = file.get_basename()
        log.debug("_on_dev_changed: event=%s name=%s", event_type.value_nick, name)

        if event_type not in (
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.DELETED,
        ):
            return

        if name and name.startswith("video"):
            log.info("Instant hotplug event: %s %s", event_type.value_nick, name)
            self._schedule_debounced_detection()

    def _on_usb_bus_changed(
        self,
        _monitor: Gio.FileMonitor,
        file: Gio.File,
        _other_file: Gio.File | None,
        event_type: Gio.FileMonitorEvent,
    ) -> None:
        """Instant callback when a USB device is added/removed on the bus."""
        if event_type not in (
            Gio.FileMonitorEvent.CREATED,
            Gio.FileMonitorEvent.DELETED,
        ):
            return
        log.info("USB bus hotplug: %s %s", event_type.value_nick, file.get_path())
        self._schedule_debounced_detection(debounce_ms=2000)

    def _schedule_debounced_detection(self, debounce_ms: int = 800) -> None:
        """Debounce rapid device events into a single detection run."""
        if self._debounce_timer is not None:
            GLib.source_remove(self._debounce_timer)
        self._debounce_timer = GLib.timeout_add(debounce_ms, self._debounced_detect)

    def _debounced_detect(self) -> bool:
        """Fire after debounce period expires."""
        self._debounce_timer = None
        log.debug("Debounced hotplug detection triggered")
        self._snapshot_device_state()
        self.detect_cameras_async()
        return False  # one-shot

    def _poll_hotplug_loop(self, interval_s: float) -> None:
        """Persistent polling thread — checks for USB/video device changes."""
        while not self._poll_stop_event.wait(timeout=interval_s):
            if self._detecting:
                continue
            changed = False
            with self._poll_lock:
                try:
                    result = subprocess.run(
                        ["lsusb"], capture_output=True, text=True, timeout=5
                    )
                    current_usb = result.stdout
                    if current_usb != self._last_lsusb:
                        self._last_lsusb = current_usb
                        changed = True
                except Exception:
                    log.debug("USB hotplug check failed", exc_info=True)
                try:
                    video_devs = ",".join(sorted(glob.glob("/dev/video*")))
                    if video_devs != self._last_video_devs:
                        self._last_video_devs = video_devs
                        changed = True
                except Exception:
                    log.debug("Video device check failed", exc_info=True)
            if changed:
                GLib.idle_add(self.detect_cameras_async)

    def close(self) -> None:
        self._closed = True
        self._detection_generation += 1
        if self._detection_task:
            self._detection_task.cancel()
        self.stop_hotplug()
