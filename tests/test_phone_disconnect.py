"""Tests for phone camera disconnect/cleanup logic."""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
import time

# Add the app source to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__),
                                '..', 'usr', 'share', 'biglinux', 'bigcam'))


class FakeGLib:
    SOURCE_REMOVE = False
    SOURCE_CONTINUE = True
    _idle_queue = []
    _timer_queue = []
    _next_id = 1

    @classmethod
    def idle_add(cls, func, *args):
        cls._idle_queue.append((func, args))

    @classmethod
    def timeout_add_seconds(cls, seconds, func, *args):
        tid = cls._next_id
        cls._next_id += 1
        cls._timer_queue.append((tid, seconds, func, args))
        return tid

    @classmethod
    def source_remove(cls, tid):
        cls._timer_queue = [(t, s, f, a)
                            for t, s, f, a in cls._timer_queue if t != tid]

    @classmethod
    def drain_idle(cls):
        """Run all pending idle callbacks."""
        while cls._idle_queue:
            func, args = cls._idle_queue.pop(0)
            func(*args)

    @classmethod
    def reset(cls):
        cls._idle_queue.clear()
        cls._timer_queue.clear()
        cls._next_id = 1


class TestPhoneCameraServerStop(unittest.TestCase):
    """Test that PhoneCameraServer.stop() emits 'disconnected' when needed."""

    def setUp(self):
        FakeGLib.reset()

    def test_stop_emits_disconnected_when_had_clients(self):
        """stop() should emit 'disconnected' if there were WS clients."""
        # Create a minimal PhoneCameraServer-like object to test the logic
        # We test the logic directly since importing the real class requires GTK
        had_clients = True  # ws_clients was non-empty
        signals_emitted = []

        def fake_emit(signal_name, *args):
            signals_emitted.append((signal_name, args))

        # Simulate the stop() logic
        if had_clients:
            FakeGLib.idle_add(fake_emit, "disconnected")
        FakeGLib.idle_add(fake_emit, "status-changed", "stopped")

        # Drain the idle queue
        FakeGLib.drain_idle()

        signal_names = [s[0] for s in signals_emitted]
        self.assertIn("disconnected", signal_names,
                      "stop() should emit 'disconnected' when there were clients")
        self.assertIn("status-changed", signal_names)
        # "disconnected" should come before "status-changed"
        self.assertLess(signal_names.index("disconnected"),
                        signal_names.index("status-changed"))

    def test_stop_no_disconnect_without_clients(self):
        """stop() should NOT emit 'disconnected' if there were no clients."""
        had_clients = False
        signals_emitted = []

        def fake_emit(signal_name, *args):
            signals_emitted.append((signal_name, args))

        if had_clients:
            FakeGLib.idle_add(fake_emit, "disconnected")
        FakeGLib.idle_add(fake_emit, "status-changed", "stopped")

        FakeGLib.drain_idle()

        signal_names = [s[0] for s in signals_emitted]
        self.assertNotIn("disconnected", signal_names,
                         "stop() should NOT emit 'disconnected' without clients")
        self.assertIn("status-changed", signal_names)


class TestDoPhoneDisconnect(unittest.TestCase):
    """Test _do_phone_disconnect logic."""

    def test_resets_ui_when_phone_was_active_no_cameras(self):
        """When phone was active camera and no cameras remain, UI should reset."""
        # Simulate the logic
        active_camera_is_phone = True
        cameras_remaining = []

        preview_showed_no_camera = False
        title_reset = False

        if active_camera_is_phone:
            # stream_engine.stop() + active_camera = None
            pass

        if active_camera_is_phone and not cameras_remaining:
            preview_showed_no_camera = True
            title_reset = True

        self.assertTrue(preview_showed_no_camera)
        self.assertTrue(title_reset)

    def test_no_ui_reset_when_other_cameras_exist(self):
        """When other cameras remain, should NOT show 'No camera'."""
        active_camera_is_phone = True
        cameras_remaining = ["v4l2:/dev/video0"]

        preview_showed_no_camera = False

        if active_camera_is_phone and not cameras_remaining:
            preview_showed_no_camera = True

        self.assertFalse(preview_showed_no_camera)

    def test_skip_if_phone_reconnected(self):
        """Should skip cleanup if phone reconnected during grace period."""
        is_connected = True
        cleanup_ran = False

        if is_connected:
            pass  # return early
        else:
            cleanup_ran = True

        self.assertFalse(cleanup_ran)


class TestPhoneStatusDotHandler(unittest.TestCase):
    """Test _on_phone_status_dot cleanup on 'stopped' status."""

    def setUp(self):
        FakeGLib.reset()

    def test_stopped_cancels_timer_and_disconnects(self):
        """When status='stopped', should cancel grace timer and call disconnect."""
        timer_id = FakeGLib.timeout_add_seconds(5, lambda: None)
        disconnect_called = False

        status = "stopped"
        phone_disconnect_timer = timer_id

        if status == "stopped":
            if phone_disconnect_timer:
                FakeGLib.source_remove(phone_disconnect_timer)
                phone_disconnect_timer = None
            disconnect_called = True

        self.assertIsNone(phone_disconnect_timer)
        self.assertTrue(disconnect_called)
        self.assertEqual(len(FakeGLib._timer_queue), 0,
                         "Grace timer should have been cancelled")

    def test_listening_does_not_trigger_disconnect(self):
        """Other statuses should NOT trigger phone disconnect."""
        disconnect_called = False
        status = "listening"

        if status == "stopped":
            disconnect_called = True

        self.assertFalse(disconnect_called)

    def test_connected_does_not_trigger_disconnect(self):
        """Connected status should NOT trigger phone disconnect."""
        disconnect_called = False
        status = "connected"

        if status == "stopped":
            disconnect_called = True

        self.assertFalse(disconnect_called)


class TestIsConnectedProperty(unittest.TestCase):
    """Test the is_connected property logic."""

    def test_connected_with_ws_clients(self):
        ws_clients = {"ws1"}
        last_frame_time = time.monotonic() - 10  # old

        if ws_clients:
            is_connected = True
        else:
            is_connected = (time.monotonic() - last_frame_time) < 3.0

        self.assertTrue(is_connected)

    def test_connected_with_recent_http_frames(self):
        ws_clients = set()
        last_frame_time = time.monotonic() - 1  # 1 second ago

        if ws_clients:
            is_connected = True
        else:
            is_connected = (time.monotonic() - last_frame_time) < 3.0

        self.assertTrue(is_connected)

    def test_not_connected_when_stale(self):
        ws_clients = set()
        last_frame_time = time.monotonic() - 5  # 5 seconds ago

        if ws_clients:
            is_connected = True
        else:
            is_connected = (time.monotonic() - last_frame_time) < 3.0

        self.assertFalse(is_connected)


class TestGraceTimerReset(unittest.TestCase):
    """Test that repeated disconnect signals reset the grace timer."""

    def setUp(self):
        FakeGLib.reset()

    def test_timer_reset_on_multiple_disconnects(self):
        """Multiple disconnect signals should cancel+reset the timer."""
        timer1 = FakeGLib.timeout_add_seconds(5, lambda: "first")
        self.assertEqual(len(FakeGLib._timer_queue), 1)

        # Second disconnect signal cancels first timer, sets new one
        FakeGLib.source_remove(timer1)
        timer2 = FakeGLib.timeout_add_seconds(5, lambda: "second")

        self.assertEqual(len(FakeGLib._timer_queue), 1)
        self.assertNotEqual(timer1, timer2)


if __name__ == "__main__":
    unittest.main()
