"""Bounded background I/O with cancellable, single-delivery GTK callbacks."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import logging
import threading
from typing import Any, Callable

from gi.repository import GLib

log = logging.getLogger(__name__)
# Do not create a new OS thread for every slider event or thumbnail.
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="bigcam-io")
_SLOTS = threading.BoundedSemaphore(64)


class TaskHandle:
    """Cancellation also suppresses callbacks queued before the cancellation.

    Running system calls are not interruptible; they must have finite timeouts.
    A caller disposing a widget/camera should cancel its associated handles.
    """
    def __init__(self):
        self._cancelled = threading.Event()
        self.future: Future | None = None

    def cancel(self) -> None:
        self._cancelled.set()
        if self.future is not None:
            self.future.cancel()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def deliver(self, callback, value) -> bool:
        if callback is not None and not self.cancelled:
            try:
                callback(value)
            except Exception:
                log.exception("Background task result handler failed")
        # GLib callbacks must not repeat when an application callback is truthy.
        return GLib.SOURCE_REMOVE


def run_async(task: Callable[..., Any], args: tuple = (),
              on_success: Callable[[Any], None] | None = None,
              on_error: Callable[[Exception], None] | None = None) -> TaskHandle:
    """Submit without blocking the main loop; return a cancellation handle."""
    handle = TaskHandle()
    if not _SLOTS.acquire(blocking=False):
        error = RuntimeError("BigCam background task queue is full")
        log.warning("%s", error)
        if on_error:
            GLib.idle_add(handle.deliver, on_error, error)
        return handle

    def completed(future):
        try:
            if future.cancelled() or handle.cancelled:
                return
            error = future.exception()
            if error is None:
                if on_success:
                    GLib.idle_add(handle.deliver, on_success, future.result())
            elif on_error:
                GLib.idle_add(handle.deliver, on_error, error)
            else:
                log.error("Background task failed", exc_info=(type(error), error, error.__traceback__))
        finally:
            _SLOTS.release()

    try:
        handle.future = _POOL.submit(task, *args)
        handle.future.add_done_callback(completed)
    except RuntimeError as error:
        _SLOTS.release()
        GLib.idle_add(handle.deliver, on_error, error)
    return handle


def shutdown_workers() -> None:
    """Cancel queued work; running tasks retain their finite I/O timeouts."""
    _POOL.shutdown(wait=False, cancel_futures=True)
