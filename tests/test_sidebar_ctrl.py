"""SidebarController: the Ctrl+1..Ctrl+5 shortcuts must actually switch pages."""

from __future__ import annotations

import pytest

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

if not Gtk.init_check():
    pytest.skip("no display available", allow_module_level=True)

from ui.controllers.sidebar_ctrl import SidebarController  # noqa: E402


@pytest.fixture
def controller():
    split = Adw.OverlaySplitView()
    pages = {
        "controls": (Gtk.Box(), "Controls", "adjustlevels"),
        "effects": (Gtk.Box(), "Effects", "draw-watercolor"),
        "gallery": (Gtk.Box(), "Photos", "view-list-images"),
        "videos": (Gtk.Box(), "Videos", "view-list-video"),
        "settings": (Gtk.Box(), "Settings", "configure"),
    }
    return SidebarController(split, pages)


def test_page_count_matches_registered_pages(controller):
    assert controller.page_count == 5


@pytest.mark.parametrize(
    "index,name",
    [(0, "controls"), (1, "effects"), (2, "gallery"), (3, "videos"), (4, "settings")],
)
def test_show_page_switches_the_stack(controller, index, name):
    assert controller.show_page(index) is True
    assert controller._view_stack.get_visible_child_name() == name


def test_show_page_also_updates_the_tab_buttons(controller):
    controller.show_page(3)
    active = [i for i, b in enumerate(controller._sidebar_tab_btns) if b.get_active()]
    assert active == [3], "tab bar got out of sync with the ViewStack"


def test_show_page_rejects_out_of_range(controller):
    assert controller.show_page(99) is False
    assert controller.show_page(-1) is False


def test_window_delegates_instead_of_owning_a_view_stack():
    """window.py must not reference a _view_stack it never creates."""
    import inspect

    from ui import window as window_mod

    src = inspect.getsource(window_mod)
    assert "self._view_stack" not in src, (
        "window.py still references self._view_stack (AttributeError on Ctrl+N)"
    )
