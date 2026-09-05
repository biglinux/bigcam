"""Unit tests for the global EventBus."""

import sys
import os

# Add the src path so we can import modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../usr/share/biglinux/bigcam')))

from core.event_bus import event_bus

def test_event_bus_singleton():
    """Ensure the EventBus is a singleton."""
    from core.event_bus import EventBus
    bus1 = EventBus()
    bus2 = EventBus()
    assert bus1 is bus2
    assert bus1 is event_bus

def test_event_bus_emit_camera_changed():
    """Test emitting the camera-changed signal."""
    emitted = False
    received_cam = None

    def on_camera_changed(bus, cam_info):
        nonlocal emitted, received_cam
        emitted = True
        received_cam = cam_info

    handler_id = event_bus.connect("camera-changed", on_camera_changed)
    event_bus.emit("camera-changed", "fake_camera_info")

    assert emitted is True
    assert received_cam == "fake_camera_info"

    event_bus.disconnect(handler_id)

def test_event_bus_emit_mobile_status():
    """Test emitting the mobile-status-changed signal."""
    emitted = False
    received_backend = None
    received_status = None

    def on_mobile_status(bus, backend, status):
        nonlocal emitted, received_backend, received_status
        emitted = True
        received_backend = backend
        received_status = status

    handler_id = event_bus.connect("mobile-status-changed", on_mobile_status)
    event_bus.emit("mobile-status-changed", "phone", "connected")

    assert emitted is True
    assert received_backend == "phone"
    assert received_status == "connected"

    event_bus.disconnect(handler_id)
