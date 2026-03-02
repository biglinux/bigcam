"""Camera selector – DropDown with backend icon + status."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk, Gio, GObject

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
    BackendType.PHONE: "phone-symbolic",
}


class _CameraItem(GObject.Object):
    """Model item holding camera info."""

    name = GObject.Property(type=str, default="")

    def __init__(self, camera: CameraInfo) -> None:
        super().__init__()
        self.camera = camera
        self.name = camera.name
        self.icon = _BACKEND_ICONS.get(camera.backend, "camera-web-symbolic")


class CameraSelector(Gtk.Box):
    """Camera dropdown that fits in a HeaderBar."""

    __gsignals__ = {
        "camera-selected": (GObject.SignalFlags.RUN_LAST, None, (object,)),
    }

    def __init__(self, camera_manager: CameraManager) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._manager = camera_manager
        self._cameras: list[CameraInfo] = []
        self._model = Gio.ListStore.new(_CameraItem)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_factory_setup)
        factory.connect("bind", self._on_factory_bind)

        self._dropdown = Gtk.DropDown(
            model=self._model, factory=factory, enable_search=True
        )
        self._dropdown.set_tooltip_text(_("Select camera"))
        self._dropdown.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("Camera selector")]
        )

        # Expression for search
        expr = Gtk.PropertyExpression.new(_CameraItem, None, "name")
        self._dropdown.set_expression(expr)

        self._dropdown.connect("notify::selected", self._on_selected)
        self.append(self._dropdown)

        self._manager.connect("cameras-changed", self._on_cameras_changed)

    @staticmethod
    def _on_factory_setup(
        _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem
    ) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon = Gtk.Image()
        label = Gtk.Label(xalign=0)
        box.append(icon)
        box.append(label)
        list_item.set_child(box)

    @staticmethod
    def _on_factory_bind(
        _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem
    ) -> None:
        item: _CameraItem = list_item.get_item()
        box: Gtk.Box = list_item.get_child()
        icon: Gtk.Image = box.get_first_child()
        label: Gtk.Label = icon.get_next_sibling()
        icon.set_from_icon_name(item.icon)
        label.set_label(item.name)

    def _on_cameras_changed(self, _manager: CameraManager) -> None:
        old_cam = self.selected_camera
        self._cameras = self._manager.cameras
        self._model.remove_all()
        for cam in self._cameras:
            self._model.append(_CameraItem(cam))
        if self._cameras:
            # Try to preserve current selection
            restore_idx = 0
            if old_cam:
                restore_idx = next(
                    (i for i, c in enumerate(self._cameras) if c.id == old_cam.id),
                    0,
                )
            self._dropdown.set_selected(restore_idx)

    def _on_selected(self, *_args) -> None:
        idx = self._dropdown.get_selected()
        if 0 <= idx < len(self._cameras):
            self.emit("camera-selected", self._cameras[idx])

    @property
    def selected_camera(self) -> CameraInfo | None:
        idx = self._dropdown.get_selected()
        if 0 <= idx < len(self._cameras):
            return self._cameras[idx]
        return None
