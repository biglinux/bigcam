"""The rewritten WebSocket receive loop, driven end to end.

_FramePump is unit-tested separately; this exercises _handle_ws itself, which
is where the coalescing has to actually be wired up.  A fake WebSocket feeds
it messages and a fake decode stands in for OpenCV, so the test runs without
a phone, a network or a JPEG.

What matters: the loop must return to the socket instead of awaiting each
decode, the newest frame must be the one delivered, and audio must still go
through untouched on the same connection.
"""

from __future__ import annotations

import asyncio
import sys
import time

import numpy as np
import pytest

from core.phone_camera import PhoneCameraServer


class _Msg:
    def __init__(self, data, type_):
        self.data = data
        self.type = type_


class _FakeWS:
    """Async-iterable stand-in for aiohttp's WebSocketResponse."""

    def __init__(self, messages, interval=0.0):
        self._messages = list(messages)
        self._interval = interval
        self.prepared = False
        self.closed = False

    async def prepare(self, _request):
        self.prepared = True

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for msg in self._messages:
            # interval=0 is a burst: everything is already queued, as after a
            # Wi-Fi stall.  A non-zero interval is a steady arrival rate.
            await asyncio.sleep(self._interval)
            yield msg

    async def close(self):
        self.closed = True


class _FakeRequest:
    remote = "192.168.0.20"

    def __init__(self, token):
        self.query = {"token": token}


@pytest.fixture
def server(monkeypatch):
    srv = PhoneCameraServer()
    # GLib.idle_add would need a running main loop.
    from core import phone_camera

    monkeypatch.setattr(phone_camera.GLib, "idle_add", lambda *a, **k: None)
    return srv


@pytest.fixture
def wire(server, monkeypatch):
    """Patch the transport, record what the frame callback receives."""
    from core import phone_camera

    received: list[bytes] = []
    server.set_frame_callback(lambda bgr: received.append(bytes(bgr[0, 0])))

    def _make(messages, decode_delay=0.0, interval=0.0):
        ws = _FakeWS(messages, interval)
        monkeypatch.setattr(
            phone_camera.web, "WebSocketResponse", lambda **k: ws
        )
        # _handle_ws imports cv2 locally, so replacing the module is enough.
        # The delay is a real blocking sleep: it runs in the executor thread,
        # exactly where a slow imdecode would.
        monkeypatch.setitem(sys.modules, "cv2", _FakeCv2(decode_delay))

        async def _handle():
            return await server._handle_ws(_FakeRequest(server._token))

        return _handle, received

    return _make


class _FakeCv2:
    """Minimal stand-in: encodes the payload into the first pixel."""

    IMREAD_COLOR = 1

    def __init__(self, delay: float) -> None:
        self._delay = delay

    def imdecode(self, arr, _flags):
        if self._delay:
            time.sleep(self._delay)
        data = arr.tobytes()
        frame = np.zeros((4, 4, 3), dtype=np.uint8)
        frame[0, 0] = [data[2], 0, 0]      # the id byte after the SOI marker
        return frame


def _video(ident: int) -> _Msg:
    """A frame carrying *ident*, behind a real JPEG start-of-image marker.

    The protocol multiplexes audio onto the same socket by tagging it with a
    leading 0x01 byte.  That is safe only because a JPEG always starts 0xFF
    0xD8 — a test payload beginning with 0x01 is silently parsed as PCM.
    """
    from aiohttp import web

    return _Msg(b"\xff\xd8" + bytes([ident]), web.WSMsgType.BINARY)


def _audio(pcm: bytes) -> _Msg:
    from aiohttp import web

    return _Msg(b"\x01" + pcm, web.WSMsgType.BINARY)


# -- the frames that come out ----------------------------------------------


def test_a_single_frame_is_delivered(wire):
    handle, received = wire([_video(30)])
    asyncio.run(handle())
    assert [r[0] for r in received] == [30]


def test_the_newest_frame_wins_when_decoding_is_slow(wire):
    frames = [_video(i) for i in range(1, 21)]
    handle, received = wire(frames, decode_delay=0.01)
    asyncio.run(handle())

    assert received, "no frame was delivered at all"
    assert len(received) < 20, (
        f"decoded all {len(received)} frames despite a slow decode — the "
        f"preview would drift further behind with every one"
    )
    assert received[-1][0] == 20, (
        f"finished on frame {received[-1][0]} instead of the newest; the "
        f"preview is showing stale video"
    )


def test_frames_are_delivered_in_order(wire):
    frames = [_video(i) for i in range(1, 11)]
    handle, received = wire(frames, decode_delay=0.005)
    asyncio.run(handle())
    order = [r[0] for r in received]
    assert order == sorted(order), f"delivered out of order: {order}"


def test_nothing_is_dropped_when_the_machine_keeps_up(wire):
    """At a realistic arrival rate every frame should get through.

    Frames arriving every 20 ms against a decode that costs nothing is the
    normal case; coalescing must not throw work away when there is no
    backlog to shed.
    """
    frames = [_video(i) for i in range(1, 6)]
    handle, received = wire(frames, interval=0.02)
    asyncio.run(handle())
    assert [r[0] for r in received] == [1, 2, 3, 4, 5]


# -- the other things on the same connection -------------------------------


def test_audio_packets_are_not_treated_as_video(wire, server):
    pushed: list[bytes] = []
    server._push_audio_data = pushed.append
    handle, received = wire([_audio(b"pcmdata"), _video(7)])
    asyncio.run(handle())
    assert pushed == [b"pcmdata"]
    assert [r[0] for r in received] == [7]


def test_empty_messages_are_ignored(wire):
    handle, received = wire([_Msg(b"", __import__('aiohttp').web.WSMsgType.BINARY), _video(3)])
    asyncio.run(handle())
    assert [r[0] for r in received] == [3]


def test_a_close_message_ends_the_loop(wire):
    from aiohttp import web

    handle, received = wire(
        [_video(1), _Msg(None, web.WSMsgType.CLOSE),
         _video(2)]
    )
    asyncio.run(handle())
    assert [r[0] for r in received] == [1], "kept reading after CLOSE"


def test_an_unauthorised_request_is_rejected(server):
    from aiohttp import web

    with pytest.raises(web.HTTPUnauthorized):
        asyncio.run(server._handle_ws(_FakeRequest("wrong-token")))


# -- teardown --------------------------------------------------------------


def test_the_client_is_removed_on_disconnect(wire, server):
    handle, _received = wire([_video(3)])
    asyncio.run(handle())
    assert server._ws_clients == set()
    assert server._width == 0 and server._height == 0
