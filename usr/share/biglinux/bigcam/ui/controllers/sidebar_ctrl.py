"""Sidebar Controller managing the right-side overlay viewstack."""

from __future__ import annotations

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, Adw

from constants import APP_NAME, APP_ICON

class SidebarController:
    """Manages the Sidebar ViewStack and Header."""

    def __init__(self, split_view: Adw.OverlaySplitView, stack_pages: dict[str, tuple[Gtk.Widget, str, str]]):
        self._split_view = split_view
        self._sidebar_tab_btns: list[Gtk.ToggleButton] = []
        self._view_stack = Adw.ViewStack()
        self._view_stack.set_vexpand(True)
        self._build(stack_pages)

    def _build(self, stack_pages: dict[str, tuple[Gtk.Widget, str, str]]) -> None:
        sidebar_outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        sidebar.add_css_class("sidebar-panel")
        sidebar.set_hexpand(True)
        sidebar_outer.append(sidebar)

        drag_handle = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        drag_handle.set_size_request(6, -1)
        drag_handle.set_cursor(Gdk.Cursor.new_from_name("col-resize"))
        drag_handle.add_css_class("sidebar-drag-handle")
        drag_gesture = Gtk.GestureDrag()
        drag_gesture.connect("drag-update", self._on_sidebar_drag)
        drag_handle.add_controller(drag_gesture)

        # Add pages to ViewStack
        for page_name, (widget, title, icon) in stack_pages.items():
            self._view_stack.add_titled_with_icon(widget, page_name, title, icon)

        # HeaderBar
        sidebar_header = Adw.HeaderBar()
        sidebar_header.add_css_class("flat")
        sidebar_header.add_css_class("sidebar-header")
        sidebar_header.set_show_start_title_buttons(False)
        sidebar_header.set_show_end_title_buttons(False)

        title_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        title_box.set_halign(Gtk.Align.CENTER)
        app_icon = Gtk.Image.new_from_icon_name(APP_ICON)
        app_icon.set_pixel_size(24)
        title_box.append(app_icon)
        title_label = Gtk.Label(label=APP_NAME)
        title_label.add_css_class("heading")
        title_box.append(title_label)
        sidebar_header.set_title_widget(title_box)

        close_sidebar_btn = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close_sidebar_btn.add_css_class("flat")
        close_sidebar_btn.connect("clicked", lambda _b: self._split_view.set_show_sidebar(False))
        sidebar_header.pack_end(close_sidebar_btn)

        sidebar.append(sidebar_header)
        sidebar.append(self._view_stack)

        # Tab Bar
        tab_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0, homogeneous=True)
        tab_bar.add_css_class("sidebar-tab-bar")

        group_btn = None
        for page_name, (_, title, icon_name) in stack_pages.items():
            btn = Gtk.ToggleButton()
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.set_halign(Gtk.Align.CENTER)
            icon = Gtk.Image.new_from_icon_name(icon_name)
            icon.set_pixel_size(24)
            box.append(icon)
            label = Gtk.Label(label=title)
            label.add_css_class("caption")
            box.append(label)
            btn.set_child(box)
            btn.add_css_class("flat")
            btn.add_css_class("sidebar-tab-btn")
            if group_btn:
                btn.set_group(group_btn)
            else:
                group_btn = btn
                btn.set_active(True)
            btn.connect("toggled", self._on_sidebar_tab_toggled, page_name)
            tab_bar.append(btn)
            self._sidebar_tab_btns.append(btn)

        sidebar.append(Gtk.Separator())
        sidebar.append(tab_bar)
        sidebar_outer.append(drag_handle)

        self._split_view.set_sidebar(sidebar_outer)

    # -- public API ----------------------------------------------------------

    @property
    def page_count(self) -> int:
        return len(self._sidebar_tab_btns)

    def show_page(self, index: int) -> bool:
        """Activate the sidebar tab at *index*.  Returns False when out of range.

        Activating the toggle button keeps the tab bar and the ViewStack in
        sync — setting the stack directly would leave the buttons stale.
        """
        if not 0 <= index < len(self._sidebar_tab_btns):
            return False
        self._sidebar_tab_btns[index].set_active(True)
        return True

    def _on_sidebar_tab_toggled(self, btn: Gtk.ToggleButton, page_name: str) -> None:
        if btn.get_active():
            self._view_stack.set_visible_child_name(page_name)

    def _on_sidebar_drag(self, gesture: Gtk.GestureDrag, offset_x: float, _offset_y: float) -> None:
        # Simplistic drag-to-resize logic (ported from window.py)
        start_x, _ = gesture.get_start_point()
        current_width = self._split_view.get_sidebar_width_fraction()
        # Roughly convert pixel delta to fraction delta
        delta = -(offset_x / 1000.0)
        new_width = max(0.2, min(0.5, current_width + delta))
        self._split_view.set_sidebar_width_fraction(new_width)
