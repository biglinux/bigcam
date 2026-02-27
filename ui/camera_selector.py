"""Camera selector – DropDown with backend icon + status."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, GObject, GLib

from constants import BackendType
from core.camera_backend import CameraInfo
from core.camera_manager import CameraManager
from utils.i18n import _

_BACKEND_ICONS = {
    BackendType.V4L2: "camera-web-symbolic",
    BackendType.GPHOTO2: "camera-photo-symbolic",
    BackendType.LIBCAMERA: "camera-video-symbolic",
    BackendType.PIPEWIRE: "audio-card-symbolic",
    BackendType.IP: "network-server-symbolic",
}


class CameraSelector(Gtk.Box):
    """Camera dropdown that fits in a HeaderBar."""

    __gsignals__ = {
        "camera-selected": (GObject.SignalFlags.RUN_LAST, None, (object,)),
    }

    def __init__(self, camera_manager: CameraManager) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._manager = camera_manager
        self._cameras: list[CameraInfo] = []
        self._model = Gtk.StringList()

        self._icon = Gtk.Image.new_from_icon_name("camera-web-symbolic")
        self.append(self._icon)

        self._dropdown = Gtk.DropDown(model=self._model, enable_search=True)
        self._dropdown.set_tooltip_text(_("Select camera"))
        self._dropdown.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Camera selector")]
        )
        self._dropdown.connect("notify::selected", self._on_selected)
        self.append(self._dropdown)

        self._manager.connect("cameras-changed", self._on_cameras_changed)

    def _on_cameras_changed(self, _manager: CameraManager) -> None:
        self._cameras = self._manager.cameras
        self._model.splice(0, self._model.get_n_items(), [c.name for c in self._cameras])
        if self._cameras:
            self._dropdown.set_selected(0)

    def _on_selected(self, *_args) -> None:
        idx = self._dropdown.get_selected()
        if 0 <= idx < len(self._cameras):
            cam = self._cameras[idx]
            icon_name = _BACKEND_ICONS.get(cam.backend, "camera-web-symbolic")
            self._icon.set_from_icon_name(icon_name)
            self.emit("camera-selected", cam)

    @property
    def selected_camera(self) -> CameraInfo | None:
        idx = self._dropdown.get_selected()
        if 0 <= idx < len(self._cameras):
            return self._cameras[idx]
        return None
