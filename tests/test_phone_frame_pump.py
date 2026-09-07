"""Under load the preview must lose frames, not fall behind.

The receive loop used to await the JPEG decode inline:

    bgr = await loop.run_in_executor(None, _decode_jpeg, bytes(data))

While that ran, nothing read the socket, so aiohttp queued the messages
arriving behind it.  Every frame was still decoded and still displayed — just
progressively later.  On a machine that could not sustain the rate the
preview drifted further behind for as long as the load lasted, and never
recovered while frames kept coming.  That unbounded drift is what the stutter
over Wi-Fi was; the congestion ladder on the phone could not fix it, because
the phone was not the side falling behind.

A live preview gains nothing from a superseded frame.  _FramePump keeps only
the newest, so latency is bounded by one frame regardless of how slow the
machine is.
"""

from __future__ import annotations

import asyncio

import pytest

from core.phone_camera import _FramePump


@pytest.fixture
def pump():
    return _FramePump()


# -- the contract ----------------------------------------------------------


def test_the_first_frame_asks_for_a_pump(pump):
    assert pump.offer(b"a") is True


def test_a_second_frame_does_not_start_another_pump(pump):
    pump.offer(b"a")
    assert pump.offer(b"b") is False, "two pumps would decode in parallel"


def test_only_the_newest_frame_survives(pump):
    pump.offer(b"old")
    pump.offer(b"newer")
    pump.offer(b"newest")
    assert pump.take() == b"newest"
    assert pump.take() is None


def test_superseded_frames_are_counted(pump):
    for data in (b"a", b"b", b"c"):
        pump.offer(data)
    assert pump.superseded == 2, "dropped frames should be visible in the log"


def test_taking_twice_yields_nothing_the_second_time(pump):
    pump.offer(b"a")
    assert pump.take() == b"a"
    assert pump.take() is None


def test_a_finished_pump_can_be_restarted(pump):
    pump.offer(b"a")
    pump.take()
    pump.finish()
    assert pump.offer(b"b") is True


def test_running_reflects_the_pump_state(pump):
    assert pump.running is False
    pump.offer(b"a")
    assert pump.running is True
    pump.finish()
    assert pump.running is False


def test_an_empty_frame_is_still_a_frame(pump):
    """b"" is falsy; the sentinel for "nothing pending" must be None."""
    assert pump.offer(b"") is True
    assert pump.take() == b""


# -- latency is bounded, which is the whole point --------------------------


def test_a_slow_consumer_does_not_accumulate_a_backlog(pump):
    """Offer 100 frames while the pump handles one: 99 must be discarded."""
    decoded = []

    async def _exercise():
        async def _pump():
            try:
                while True:
                    data = pump.take()
                    if data is None:
                        return
                    await asyncio.sleep(0.005)   # a slow decode
                    decoded.append(data)
            finally:
                pump.finish()

        tasks = []
        for i in range(100):
            if pump.offer(f"f{i}".encode()):
                tasks.append(asyncio.create_task(_pump()))
            await asyncio.sleep(0)               # let the reader yield
        await asyncio.gather(*tasks)

    asyncio.run(_exercise())

    assert len(decoded) < 100, "decoded every frame; the backlog was not shed"
    assert decoded[-1] == b"f99", (
        f"last decoded frame was {decoded[-1]!r}, not the newest — the "
        f"preview is showing stale video"
    )


def test_every_offered_frame_is_either_decoded_or_counted(pump):
    """Nothing may vanish silently: the arithmetic has to add up."""
    decoded = []

    async def _exercise():
        async def _pump():
            try:
                while True:
                    data = pump.take()
                    if data is None:
                        return
                    await asyncio.sleep(0)
                    decoded.append(data)
            finally:
                pump.finish()

        tasks = []
        for i in range(50):
            if pump.offer(f"f{i}".encode()):
                tasks.append(asyncio.create_task(_pump()))
            await asyncio.sleep(0)
        await asyncio.gather(*tasks)

    asyncio.run(_exercise())
    assert len(decoded) + pump.superseded == 50


def test_no_frame_is_left_pending_after_the_pump_drains(pump):
    async def _exercise():
        async def _pump():
            try:
                while True:
                    if pump.take() is None:
                        return
                    await asyncio.sleep(0)
            finally:
                pump.finish()

        task = asyncio.create_task(_pump()) if pump.offer(b"a") else None
        await asyncio.gather(task)

    asyncio.run(_exercise())
    assert pump.take() is None
    assert pump.running is False
