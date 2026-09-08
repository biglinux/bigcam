"""Mobile Device Controller handling Phone, Scrcpy, and AirPlay connections."""

from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib

from core.event_bus import event_bus
from core.phone_camera import PhoneCameraServer
from core.scrcpy_camera import ScrcpyCamera
from core.airplay_receiver import AirPlayReceiver
from core.camera_backend import CameraInfo
from constants import BackendType
from ui.phone_camera_dialog import PhoneCameraDialog

class MobileDeviceController:
    """Manages mobile device connections and UI state."""

    def __init__(self, camera_manager, immersion_controller, audio_monitor=None):
        self._camera_manager = camera_manager
        self._immersion = immersion_controller
        self._audio_monitor = audio_monitor
        self._phone_disconnect_timer = None
        
        self.phone_server = PhoneCameraServer()
        self.scrcpy_usb = ScrcpyCamera()
        self.scrcpy_wifi = ScrcpyCamera()
        self.airplay_receiver = AirPlayReceiver()

        self._signal_ids = []
        self._setup_signals()

    def _setup_signals(self):
        self._connect(self.phone_server, "connected", self._on_phone_connected)
        self._connect(self.phone_server, "disconnected", self._on_phone_disconnected)
        self._connect(self.phone_server, "status-changed", lambda s, st: event_bus.emit("mobile-status-changed", "phone", st))

        self._connect(self.scrcpy_usb, "status-changed", lambda c, st: event_bus.emit("mobile-status-changed", "scrcpy_usb", st))
        self._connect(self.scrcpy_usb, "connected", self._on_scrcpy_receiver_connected)
        self._connect(self.scrcpy_usb, "disconnected", self._on_scrcpy_receiver_disconnected)

        self._connect(self.scrcpy_wifi, "status-changed", lambda c, st: event_bus.emit("mobile-status-changed", "scrcpy_wifi", st))
        self._connect(self.scrcpy_wifi, "connected", self._on_scrcpy_receiver_connected)
        self._connect(self.scrcpy_wifi, "disconnected", self._on_scrcpy_receiver_disconnected)

        self._connect(self.airplay_receiver, "status-changed", lambda r, st: event_bus.emit("mobile-status-changed", "airplay", st))
        self._connect(self.airplay_receiver, "connected", self._on_airplay_receiver_connected)
        self._connect(self.airplay_receiver, "disconnected", self._on_airplay_receiver_disconnected)

    def show_dialog(self, parent_window):
        """Shows the mobile connection dialog."""
        dialog = PhoneCameraDialog(
            server=self.phone_server,
            scrcpy_usb=self.scrcpy_usb,
            scrcpy_wifi=self.scrcpy_wifi,
            airplay=self.airplay_receiver,
        )
        self._immersion.present_dialog(dialog, parent_window)

    def _on_phone_connected(self, server: PhoneCameraServer, width: int, height: int) -> None:
        if self._phone_disconnect_timer:
            GLib.source_remove(self._phone_disconnect_timer)
            self._phone_disconnect_timer = None

        phone_cam = CameraInfo(
            id="phone:websocket",
            name="BigCam Phone",
            backend=BackendType.PHONE,
            device_path="websocket",
            capabilities=["video", "audio"],
            extra={"phone_server": self.phone_server},
        )
        self._camera_manager.add_phone_camera(phone_cam)
        event_bus.emit("camera-changed", phone_cam)

        if self._audio_monitor:
            from utils.i18n import _
            self._audio_monitor.add_external_source(
                "phone_browser",
                _("Phone Mic (Browser)"),
                pid=None,
                active=True,
                volume_cb=self.phone_server.set_audio_volume,
                mute_cb=self.phone_server.set_audio_muted,
            )

    def _on_phone_disconnected(self, server: PhoneCameraServer) -> None:
        if self._phone_disconnect_timer:
            GLib.source_remove(self._phone_disconnect_timer)
        self._phone_disconnect_timer = GLib.timeout_add_seconds(5, self._do_phone_disconnect)

    def _do_phone_disconnect(self) -> bool:
        self._phone_disconnect_timer = None
        if self.phone_server and self.phone_server.is_connected:
            return False
        self._camera_manager.remove_phone_camera()
        event_bus.emit("camera-changed", None)
        if self._audio_monitor:
            self._audio_monitor.remove_external_source("phone_browser")
        return False

    def _on_scrcpy_receiver_connected(self, camera: ScrcpyCamera, width: int, height: int) -> None:
        device_id = camera.device_serial
        device_name = camera.model or device_id
        cam_info = CameraInfo(
            id=f"scrcpy:{device_id}",
            name=f"Android: {device_name}",
            backend=BackendType.SCRCPY,
            device_path=camera.v4l2_device,
            capabilities=["video", "audio"],
            formats=[],
            extra={"scrcpy_camera": camera},
        )
        self._camera_manager.add_phone_camera(cam_info)
        event_bus.emit("camera-changed", cam_info)

        if self._audio_monitor:
            self._audio_monitor.add_external_source(
                "scrcpy_usb" if camera is self.scrcpy_usb else "scrcpy_wifi", cam_info.name, pid=camera.pid)

    def _on_scrcpy_receiver_disconnected(self, camera: ScrcpyCamera) -> None:
        if self._audio_monitor:
            self._audio_monitor.remove_external_source(
                "scrcpy_usb" if camera is self.scrcpy_usb else "scrcpy_wifi")
        device_id = camera.device_serial
        if device_id:
            self._camera_manager.remove_scrcpy_camera(device_id)
        event_bus.emit("camera-changed", None)

    def _on_airplay_receiver_connected(self, receiver: AirPlayReceiver, width: int, height: int) -> None:
        cam_info = CameraInfo(
            id="airplay:uxplay",
            name="iPhone / iPad",
            backend=BackendType.AIRPLAY,
            device_path=receiver.v4l2_device,
            capabilities=["video", "audio"],
            formats=[],
            extra={"airplay_receiver": self.airplay_receiver},
        )
        self._camera_manager.add_phone_camera(cam_info)
        event_bus.emit("camera-changed", cam_info)

        if self._audio_monitor:
            self._audio_monitor.add_external_source("airplay", cam_info.name, pid=receiver.pid)

    def _on_airplay_receiver_disconnected(self, receiver: AirPlayReceiver) -> None:
        if self._audio_monitor:
            self._audio_monitor.remove_external_source("airplay")
        self._camera_manager.remove_airplay_cameras()
        event_bus.emit("camera-changed", None)


    def _connect(self, obj, signal, callback):
        self._signal_ids.append((obj, obj.connect(signal, callback)))

    def disconnect_signals(self):
        if self._phone_disconnect_timer:
            GLib.source_remove(self._phone_disconnect_timer)
            self._phone_disconnect_timer = None
        for obj, ident in self._signal_ids:
            obj.disconnect(ident)
        self._signal_ids.clear()
