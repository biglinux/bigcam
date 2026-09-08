"""Serial, latest-value-wins camera commands with explicit invalidation."""
import threading
from collections import OrderedDict
from gi.repository import GLib
from utils.async_worker import run_async


class LatestCommands:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending = OrderedDict()
        self._running = False
        self._generation = 0

    def invalidate(self):
        with self._lock:
            self._generation += 1
            self._pending.clear()

    def submit(self, key, task, success, failure):
        with self._lock:
            self._pending[key] = (self._generation, task, success, failure)
            if self._running:
                return
            self._running = True
        run_async(self._drain, on_error=self._submit_failed)

    def _submit_failed(self, error):
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
            self._running = False
        for generation, _task, _success, failure in pending:
            self._deliver(generation, failure, error)

    def _deliver(self, generation, callback, value):
        with self._lock:
            valid = generation == self._generation
        if valid and callback:
            callback(value)
        return GLib.SOURCE_REMOVE

    def _drain(self):
        while True:
            with self._lock:
                if not self._pending:
                    self._running = False
                    return
                _key, (generation, task, success, failure) = self._pending.popitem(last=False)
                if generation != self._generation:
                    continue
            try:
                value = task()
                GLib.idle_add(self._deliver, generation, success, value)
            except Exception as error:
                GLib.idle_add(self._deliver, generation, failure, error)
