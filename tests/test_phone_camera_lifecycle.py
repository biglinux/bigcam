"""PhoneCameraServer: teardown must not block the event loop, or leak.

The websocket handler runs *on* the asyncio loop.  Anything it does
synchronously stalls every other connection, and the audio teardown joins a
thread and waits on a subprocess — up to five seconds during which no frame is
decoded and no other client is served.

The QUIC path has the mirror-image problem: each video frame arrives as its own
unidirectional stream, buffered until the peer signals the end.  A stream that
is abandoned mid-frame is never finished, so its buffer is never released.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from core.phone_camera import PhoneCameraServer


@pytest.fixture
def server():
    return PhoneCameraServer()


# -- audio teardown must not stall the loop -------------------------------


def test_audio_teardown_is_offloaded(server, monkeypatch):
    """A slow stop must not hold the caller — it runs off the loop thread."""
    released = threading.Event()

    def _slow_stop():
        time.sleep(0.6)
        released.set()

    monkeypatch.setattr(server, "_shutdown_audio_blocking", _slow_stop)

    t0 = time.monotonic()
    server.stop_audio_soon()
    elapsed = time.monotonic() - t0

    assert elapsed < 0.2, (
        f"teardown blocked the caller for {elapsed:.2f}s — on the websocket "
        f"handler this stalls the whole event loop"
    )
    assert released.wait(timeout=5), "teardown never ran"


def test_audio_teardown_still_completes(server, monkeypatch):
    done = threading.Event()
    monkeypatch.setattr(server, "_shutdown_audio_blocking", done.set)
    server.stop_audio_soon()
    assert done.wait(timeout=5)


def test_repeated_teardown_requests_are_safe(server, monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_shutdown_audio_blocking", lambda: calls.append(1))
    for _ in range(5):
        server.stop_audio_soon()
    time.sleep(0.4)
    assert calls, "no teardown ran"


def test_teardown_without_audio_running_is_a_noop(server):
    server.stop_audio_soon()   # must not raise


@pytest.mark.slow
def test_websocket_finally_does_not_block_the_loop(server, monkeypatch):
    """End to end: the handler's cleanup must return to the loop promptly."""
    monkeypatch.setattr(
        server, "_shutdown_audio_blocking", lambda: time.sleep(0.6)
    )

    async def _exercise():
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        server.stop_audio_soon()
        # If the teardown ran inline, this sleep starts 0.6s late.
        await asyncio.sleep(0)
        return loop.time() - t0

    assert asyncio.run(_exercise()) < 0.2


# -- QUIC stream buffers must not accumulate ------------------------------


def test_abandoned_stream_buffers_are_reclaimed():
    from core.phone_camera import _StreamBuffers

    bufs = _StreamBuffers(max_streams=4)
    for sid in range(10):
        bufs.append(sid, b"partial frame data")
    assert len(bufs) <= 4, (
        f"{len(bufs)} buffers retained; a peer that abandons streams would "
        f"grow this without bound"
    )


def test_completed_stream_is_released():
    from core.phone_camera import _StreamBuffers

    bufs = _StreamBuffers()
    bufs.append(1, b"abc")
    assert bufs.take(1) == b"abc"
    assert len(bufs) == 0


def test_taking_an_unknown_stream_returns_nothing():
    from core.phone_camera import _StreamBuffers

    assert _StreamBuffers().take(99) == b""


def test_oldest_buffer_is_dropped_first():
    from core.phone_camera import _StreamBuffers

    bufs = _StreamBuffers(max_streams=2)
    bufs.append(1, b"first")
    bufs.append(2, b"second")
    bufs.append(3, b"third")
    assert bufs.take(1) == b"", "the oldest partial frame should have been dropped"
    assert bufs.take(3) == b"third"


def test_buffers_accumulate_across_chunks():
    from core.phone_camera import _StreamBuffers

    bufs = _StreamBuffers()
    bufs.append(7, b"hello ")
    bufs.append(7, b"world")
    assert bufs.take(7) == b"hello world"


def test_clear_releases_everything():
    from core.phone_camera import _StreamBuffers

    bufs = _StreamBuffers()
    for sid in range(3):
        bufs.append(sid, b"x")
    bufs.clear()
    assert len(bufs) == 0
