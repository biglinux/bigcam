"""Camera Manager – detects, tracks and switches between cameras from all backends."""

from __future__ import annotations

import logging
import subprocess
import threading
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

    def detect_cameras_async(self) -> None:
        """Run detection on all backends in a background thread."""
        if self._detecting:
            return
        self._detecting = True

        def _worker() -> None:
            all_cameras: list[CameraInfo] = []
            seen_ids: set[str] = set()
            try:
                for b in self._backends:
                    if b.get_backend_type() == BackendType.IP:
                        continue  # IP cameras are added manually
                    try:
                        found = b.detect_cameras()
                        if (
                            not found
                            and hasattr(b, "_streaming_active")
                            and b._streaming_active
                        ):
                            # Keep existing cameras for this backend during streaming
                            found = [
                                c
                                for c in self._cameras
                                if c.backend == b.get_backend_type()
                            ]
                        for cam in found:
                            if cam.id not in seen_ids:
                                seen_ids.add(cam.id)
                                all_cameras.append(cam)
                    except Exception as exc:
                        GLib.idle_add(self.emit, "camera-error", str(exc))
            finally:
                self._detecting = False
            GLib.idle_add(self._on_detection_done, all_cameras)

        threading.Thread(target=_worker, daemon=True).start()

    def _on_detection_done(self, cameras: list[CameraInfo]) -> bool:
        # Preserve manually-added cameras (IP, phone) across hotplug scans
        manual_backends = {BackendType.IP, BackendType.PHONE}
        manual_cameras = [c for c in self._cameras if c.backend in manual_backends]
        seen_ids = {c.id for c in cameras}
        for mc in manual_cameras:
            if mc.id not in seen_ids:
                cameras.append(mc)

        old_ids = {c.id for c in self._cameras}
        new_ids = {c.id for c in cameras}
        self._cameras = cameras
        changed = self._first_detection or old_ids != new_ids
        log.info(
            "Detection done: %d cameras, old=%s, new=%s, first=%s, emit=%s",
            len(cameras),
            old_ids,
            new_ids,
            self._first_detection,
            changed,
        )
        if changed:
            self._first_detection = False
            self.emit("cameras-changed")
        return False

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
        """Register a phone camera source (WebRTC)."""
        self._cameras = [c for c in self._cameras if c.backend != BackendType.PHONE]
        self._cameras.append(camera)
        self.emit("cameras-changed")

    def remove_phone_camera(self) -> None:
        """Remove phone camera from the list."""
        had = any(c.backend == BackendType.PHONE for c in self._cameras)
        self._cameras = [c for c in self._cameras if c.backend != BackendType.PHONE]
        if had:
            self.emit("cameras-changed")

    # -- controls proxy ------------------------------------------------------

    def get_controls(self, camera: CameraInfo) -> list[CameraControl]:
        backend = self.get_backend(camera.backend)
        if backend:
            return backend.get_controls(camera)
        return []

    def set_control(self, camera: CameraInfo, control_id: str, value: Any) -> bool:
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

    def get_gst_source(
        self, camera: CameraInfo, fmt: VideoFormat | None = None,
        prefer_v4l2: bool = False,
    ) -> str:
        backend = self.get_backend(camera.backend)
        if backend:
            try:
                return backend.get_gst_source(camera, fmt, prefer_v4l2=prefer_v4l2)
            except TypeError:
                return backend.get_gst_source(camera, fmt)
        return ""

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

        # Keep polling as a safety-net fallback
        if self._hotplug_timer is None:
            self._hotplug_timer = GLib.timeout_add(interval_ms, self._poll_hotplug)

    def stop_hotplug(self) -> None:
        # Cancel pending debounce
        if self._debounce_timer is not None:
            GLib.source_remove(self._debounce_timer)
            self._debounce_timer = None

        if self._hotplug_timer is not None:
            GLib.source_remove(self._hotplug_timer)
            self._hotplug_timer = None

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

    def _poll_hotplug(self) -> bool:
        """Safety-net polling fallback — runs at a longer interval."""
        if self._detecting:
            return True

        def _check_changes() -> None:
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

        threading.Thread(target=_check_changes, daemon=True).start()
        return True
