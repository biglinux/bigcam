"""Camera selector – DropDown with backend icon + status."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Gtk, Gio, GLib, GObject

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
    active = GObject.Property(type=bool, default=False)

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

        self._sig_selected = self._dropdown.connect(
            "notify::selected", self._on_selected
        )
        # Track confirmed camera ID to ignore GTK's internal selection bounces
        self._confirmed_cam_id: str | None = None
        self._selection_idle_pending = False
        self.append(self._dropdown)

        self._manager.connect("cameras-changed", self._on_cameras_changed)

    @staticmethod
    def _on_factory_setup(
        _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem
    ) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        icon = Gtk.Image()
        label = Gtk.Label(xalign=0, hexpand=True)
        status = Gtk.Image.new_from_icon_name("media-playback-start-symbolic")
        status.add_css_class("success")
        status.set_pixel_size(12)
        status.set_visible(False)
        box.append(icon)
        box.append(label)
        box.append(status)
        list_item.set_child(box)

    @staticmethod
    def _on_factory_bind(
        _factory: Gtk.SignalListItemFactory, list_item: Gtk.ListItem
    ) -> None:
        item: _CameraItem = list_item.get_item()
        box: Gtk.Box = list_item.get_child()
        icon: Gtk.Image = box.get_first_child()
        label: Gtk.Label = icon.get_next_sibling()
        status: Gtk.Image = label.get_next_sibling()
        icon.set_from_icon_name(item.icon)
        label.set_label(item.name)
        status.set_visible(item.active)

    def _on_cameras_changed(self, _manager: CameraManager) -> None:
        import logging
        log = logging.getLogger(__name__)
        old_cam = self.selected_camera
        self._cameras = self._manager.cameras
        log.info("_on_cameras_changed: %d cameras, old=%s", len(self._cameras), old_cam.name if old_cam else None)

        # Block handler completely during rebuild
        self._dropdown.handler_block(self._sig_selected)
        self._model.remove_all()
        for cam in self._cameras:
            self._model.append(_CameraItem(cam))
        if self._cameras:
            restore_idx = 0
            if old_cam:
                restore_idx = next(
                    (i for i, c in enumerate(self._cameras) if c.id == old_cam.id),
                    0,
                )
            self._dropdown.set_selected(restore_idx)
            # Mark this as the confirmed selection — any bounce back to this is ignored
            self._confirmed_cam_id = self._cameras[restore_idx].id
        self._dropdown.handler_unblock(self._sig_selected)
        log.info("_on_cameras_changed: done, confirmed=%s", self._confirmed_cam_id)

    def set_selected_silent(self, idx: int) -> None:
        """Set dropdown selection without emitting camera-selected signal."""
        self._dropdown.handler_block(self._sig_selected)
        self._dropdown.set_selected(idx)
        if 0 <= idx < len(self._cameras):
            self._confirmed_cam_id = self._cameras[idx].id
        self._dropdown.handler_unblock(self._sig_selected)

    def block_signals(self) -> None:
        """Block dropdown selection signals (use during camera setup)."""
        self._dropdown.handler_block(self._sig_selected)

    def unblock_signals(self) -> None:
        """Unblock dropdown selection signals and update confirmed state."""
        idx = self._dropdown.get_selected()
        if 0 <= idx < len(self._cameras):
            self._confirmed_cam_id = self._cameras[idx].id
        self._selection_idle_pending = False
        self._dropdown.handler_unblock(self._sig_selected)

    def _on_selected(self, *_args) -> None:
        """Handle dropdown selection change.

        Uses idle-debounce: GTK may emit multiple notify::selected during
        internal reprocessing (selection bounces).  We schedule ONE idle
        callback.  By the time it runs, the dropdown has stabilised.
        Only if the final selection is different from the confirmed camera
        do we actually emit camera-selected (= real user interaction).
        """
        if not self._selection_idle_pending:
            self._selection_idle_pending = True
            GLib.idle_add(self._process_selection)

    def _process_selection(self) -> bool:
        import logging
        log = logging.getLogger(__name__)
        self._selection_idle_pending = False
        idx = self._dropdown.get_selected()
        if 0 <= idx < len(self._cameras):
            cam = self._cameras[idx]
            log.info("_process_selection: idx=%d (%s), confirmed=%s", idx, cam.name, self._confirmed_cam_id)
            if cam.id != self._confirmed_cam_id:
                self._confirmed_cam_id = cam.id
                self.emit("camera-selected", cam)
            else:
                log.info("_process_selection: same as confirmed, skipping")
        return False  # remove idle source

    @property
    def selected_camera(self) -> CameraInfo | None:
        idx = self._dropdown.get_selected()
        if 0 <= idx < len(self._cameras):
            return self._cameras[idx]
        return None

    def set_active_camera(self, camera_id: str | None) -> None:
        """Mark a camera as active (streaming) in the dropdown list."""
        for i in range(self._model.get_n_items()):
            item: _CameraItem = self._model.get_item(i)
            item.active = item.camera.id == camera_id if camera_id else False
