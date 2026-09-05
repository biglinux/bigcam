"""Every page must build without GTK criticals.

A `gtk_box_append: assertion 'gtk_widget_get_parent (child) == NULL' failed`
is not cosmetic: GTK refuses the second append, so whatever was being added
silently never appears (or appears in the wrong place).
"""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display available", allow_module_level=True)

pytestmark = pytest.mark.gtk


@pytest.fixture
def strict_containers(monkeypatch):
    """Turn "widget already has a parent" into a test failure."""
    offences: list[str] = []

    def _wrap(cls, method):
        original = getattr(cls, method)

        def _checked(self, child, *args, **kwargs):
            if child is not None and hasattr(child, "get_parent"):
                parent = child.get_parent()
                if parent is not None:
                    offences.append(
                        f"{cls.__name__}.{method}({type(child).__name__}) — "
                        f"already parented to {type(parent).__name__}"
                    )
            return original(self, child, *args, **kwargs)

        monkeypatch.setattr(cls, method, _checked)

    for cls, methods in (
        (Gtk.Box, ("append", "prepend")),
        (Gtk.Overlay, ("add_overlay",)),
    ):
        for m in methods:
            _wrap(cls, m)
    return offences


def _make_settings_page(settings):
    from core.camera_manager import CameraManager
    from ui.settings_page import SettingsPage

    class _StubEngine:
        def __init__(self):
            from core.effects import EffectPipeline

            self._effects = EffectPipeline()

        @property
        def effects(self):
            return self._effects

        @property
        def last_frame_bgr(self):
            return None

        def set_overlay_rects(self, rects):
            pass

        def set_qr_scanning(self, active):
            pass

    return SettingsPage(settings, _StubEngine(), CameraManager.__new__(CameraManager))


def test_settings_page_builds_cleanly(settings, strict_containers, monkeypatch):
    from core.camera_manager import CameraManager

    monkeypatch.setattr(CameraManager, "connect", lambda self, *a, **kw: 0)
    _make_settings_page(settings)
    assert not strict_containers, "\n".join(strict_containers)


def test_settings_page_row_order(settings, monkeypatch):
    """The virtual-camera group must come before the per-device list."""
    from core.camera_manager import CameraManager

    monkeypatch.setattr(CameraManager, "connect", lambda self, *a, **kw: 0)
    page = _make_settings_page(settings)

    # Walk the content box and collect PreferencesGroup titles in order.
    from gi.repository import Adw

    def _find_content(widget):
        if isinstance(widget, Gtk.Box):
            child = widget.get_first_child()
            while child:
                if isinstance(child, Adw.PreferencesGroup):
                    return widget
                child = child.get_next_sibling()
        child = widget.get_first_child() if hasattr(widget, "get_first_child") else None
        while child:
            found = _find_content(child)
            if found is not None:
                return found
            child = child.get_next_sibling()
        return None

    content = _find_content(page)
    assert content is not None, "could not locate the settings content box"

    titles = []
    child = content.get_first_child()
    while child:
        if isinstance(child, Adw.PreferencesGroup):
            titles.append(child.get_title())
        child = child.get_next_sibling()

    assert "Virtual Camera" in titles or any("Virtual" in t for t in titles)
    # Each group may appear only once — a duplicate means a double append.
    assert len(titles) == len(set(titles)), f"duplicate settings groups: {titles}"


def test_preview_area_builds_cleanly(strict_containers, monkeypatch):
    from core.camera_manager import CameraManager
    from core.stream_engine import StreamEngine
    from ui.preview_area import PreviewArea

    monkeypatch.setattr(CameraManager, "_register_backends", lambda self: None)
    PreviewArea(StreamEngine(CameraManager()))
    assert not strict_containers, "\n".join(strict_containers)


def test_effects_page_builds_cleanly(strict_containers):
    from core.effects import EffectPipeline
    from ui.effects_page import EffectsPage

    EffectsPage(EffectPipeline())
    assert not strict_containers, "\n".join(strict_containers)


def test_camera_controls_page_builds_cleanly(strict_containers, monkeypatch):
    from core.camera_manager import CameraManager
    from core.stream_engine import StreamEngine
    from ui.camera_controls_page import CameraControlsPage

    monkeypatch.setattr(CameraManager, "_register_backends", lambda self: None)
    mgr = CameraManager()
    page = CameraControlsPage(mgr, StreamEngine(mgr))
    page.set_camera(None)
    page.set_camera(None)  # re-entering the empty state must not double-parent
    assert not strict_containers, "\n".join(strict_containers)


def test_whole_window_builds_cleanly(settings, strict_containers, monkeypatch):
    from core.camera_manager import CameraManager
    from core.virtual_camera import VirtualCamera
    from ui import window as window_mod
    from gi.repository import Adw

    monkeypatch.setattr(CameraManager, "_register_backends", lambda self: None)
    monkeypatch.setattr(CameraManager, "detect_cameras_async",
                        lambda self, force_emit=False: None)
    monkeypatch.setattr(CameraManager, "start_hotplug",
                        lambda self, interval_ms=5000: None)
    monkeypatch.setattr(window_mod.SettingsManager, "__new__",
                        lambda cls, *a, **kw: settings)
    monkeypatch.setattr(window_mod.SettingsManager, "__init__",
                        lambda self, *a, **kw: None)
    monkeypatch.setattr(VirtualCamera, "cleanup_dynamic_devices",
                        classmethod(lambda cls: None))
    monkeypatch.setattr("core.audio_monitor.AudioMonitor.detect_all",
                        lambda self: None)

    app = Adw.Application(application_id="br.com.biglinux.bigcam.uitest")
    win = window_mod.BigDigicamWindow(app)
    try:
        assert not strict_containers, "\n".join(strict_containers)
    finally:
        win.destroy()
