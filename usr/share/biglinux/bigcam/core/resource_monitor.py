"""Lightweight resource monitor for BigCam.

Periodically samples RSS memory and CPU usage of the current process
and emits GObject signals when thresholds are exceeded. Designed to
run entirely in the GLib main loop (no extra threads) via
GLib.timeout_add_seconds.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable

from gi.repository import GLib, GObject

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Snapshot of resource usage
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ResourceSnapshot:
    """Point-in-time resource usage."""
    rss_mb: float = 0.0
    cpu_percent: float = 0.0
    timestamp: float = field(default_factory=time.monotonic)


# ---------------------------------------------------------------------------
# Feature descriptor
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class FeatureDescriptor:
    """Metadata for a feature that may consume significant resources."""
    feature_id: str
    label: str
    description: str
    is_active: Callable[[], bool]
    disable: Callable[[], None]
    # Approximate overhead when active (used for suggestions)
    estimated_cpu: float = 0.0   # percent
    estimated_ram_mb: float = 0.0
    # When False the feature is shown for informational purposes only
    # and cannot be disabled (e.g. active camera sources).
    disableable: bool = True


# ---------------------------------------------------------------------------
# ResourceMonitor
# ---------------------------------------------------------------------------

_PROC_STAT = f"/proc/{os.getpid()}/stat"
_PAGE_SIZE = os.sysconf("SC_PAGESIZE")
_CLK_TCK = os.sysconf("SC_CLK_TCK")


def _system_ram_mb() -> float:
    """Return total system RAM in MB from /proc/meminfo."""
    try:
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024  # kB → MB
    except Exception:
        pass
    return 4096.0  # safe fallback


def _adaptive_ram_threshold() -> float:
    """Set RAM threshold to ~25% of system RAM (min 400 MB, max 2000 MB)."""
    total = _system_ram_mb()
    threshold = total * 0.25
    return max(400.0, min(2000.0, threshold))


class ResourceMonitor(GObject.Object):
    """Monitor process resource usage and alert on high consumption.

    Signals
    -------
    high-resource(snapshot: ResourceSnapshot, features: list[FeatureDescriptor])
        Emitted when CPU or RAM exceeds configured thresholds and stays
        above for *sustained_seconds*.  ``features`` contains the active
        features sorted by estimated cost (highest first).
    snapshot(snap: ResourceSnapshot)
        Emitted every sample (for optional live display).
    """

    __gsignals__ = {
        "high-resource": (
            GObject.SignalFlags.RUN_LAST,
            None,
            (object, object),  # (ResourceSnapshot, list[FeatureDescriptor])
        ),
        "snapshot": (GObject.SignalFlags.RUN_LAST, None, (object,)),
    }

    def __init__(
        self,
        *,
        ram_threshold_mb: float = 0,
        cpu_threshold: float = 80.0,
        sample_interval_s: int = 5,
        sustained_seconds: int = 15,
        cooldown_seconds: int = 120,
    ) -> None:
        super().__init__()
        self._ram_threshold = ram_threshold_mb or _adaptive_ram_threshold()
        self._cpu_threshold = cpu_threshold
        self._interval = sample_interval_s
        self._sustained = sustained_seconds
        self._cooldown = cooldown_seconds

        self._features: dict[str, FeatureDescriptor] = {}
        self._timer_id: int = 0
        self._running = False

        # CPU measurement state
        self._prev_utime: int = 0
        self._prev_stime: int = 0
        self._prev_wall: float = 0.0

        # Sustained alert state
        self._alert_start: float = 0.0
        self._last_alert: float = 0.0

    # -- feature registration ------------------------------------------------

    def register_feature(self, desc: FeatureDescriptor) -> None:
        """Register a feature that can be suggested for deactivation."""
        self._features[desc.feature_id] = desc

    def unregister_feature(self, feature_id: str) -> None:
        self._features.pop(feature_id, None)

    # -- start / stop --------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._read_proc_stat()  # baseline
        self._prev_wall = time.monotonic()
        self._timer_id = GLib.timeout_add_seconds(self._interval, self._tick)
        log.info(
            "ResourceMonitor started (RAM>%.0f MB, CPU>%.0f%%, every %ds)",
            self._ram_threshold, self._cpu_threshold, self._interval,
        )

    def stop(self) -> None:
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0
        self._running = False

    # -- properties ----------------------------------------------------------

    @property
    def ram_threshold_mb(self) -> float:
        return self._ram_threshold

    @ram_threshold_mb.setter
    def ram_threshold_mb(self, value: float) -> None:
        self._ram_threshold = value

    @property
    def cpu_threshold(self) -> float:
        return self._cpu_threshold

    @cpu_threshold.setter
    def cpu_threshold(self, value: float) -> None:
        self._cpu_threshold = value

    # -- sampling ------------------------------------------------------------

    def sample(self) -> ResourceSnapshot:
        """Take a single resource snapshot (can be called externally)."""
        rss = self._read_rss_mb()
        cpu = self._read_cpu_percent()
        snap = ResourceSnapshot(rss_mb=rss, cpu_percent=cpu)
        return snap

    def _tick(self) -> bool:
        """GLib timer callback."""
        if not self._running:
            return False
        snap = self.sample()
        self.emit("snapshot", snap)

        over_limit = (
            snap.rss_mb > self._ram_threshold
            or snap.cpu_percent > self._cpu_threshold
        )
        now = time.monotonic()

        if over_limit:
            if self._alert_start == 0.0:
                self._alert_start = now
            elif (
                now - self._alert_start >= self._sustained
                and now - self._last_alert >= self._cooldown
            ):
                active = self._active_features()
                if active:
                    self._last_alert = now
                    self._alert_start = 0.0
                    log.warning(
                        "High resource usage: RSS=%.0f MB, CPU=%.0f%%, "
                        "active features: %s",
                        snap.rss_mb,
                        snap.cpu_percent,
                        [f.feature_id for f in active],
                    )
                    self.emit("high-resource", snap, active)
        else:
            self._alert_start = 0.0

        return True  # keep timer alive

    def _active_features(self) -> list[FeatureDescriptor]:
        """Return active features sorted by estimated cost (highest first)."""
        active = [f for f in self._features.values() if f.is_active()]
        active.sort(
            key=lambda f: f.estimated_cpu + f.estimated_ram_mb, reverse=True
        )
        return active

    # -- /proc reading -------------------------------------------------------

    def _read_rss_mb(self) -> float:
        """Read RSS from /proc/self/statm (fast, no subprocess)."""
        try:
            with open("/proc/self/statm", "r") as fh:
                parts = fh.readline().split()
                rss_pages = int(parts[1])
                return rss_pages * _PAGE_SIZE / (1024 * 1024)
        except Exception:
            return 0.0

    def _read_proc_stat(self) -> tuple[int, int]:
        """Read utime + stime from /proc/<pid>/stat."""
        try:
            with open(_PROC_STAT, "r") as fh:
                parts = fh.readline().split()
                utime = int(parts[13])
                stime = int(parts[14])
                self._prev_utime = utime
                self._prev_stime = stime
                return utime, stime
        except Exception:
            return 0, 0

    def _read_cpu_percent(self) -> float:
        """Calculate CPU % since last sample."""
        try:
            with open(_PROC_STAT, "r") as fh:
                parts = fh.readline().split()
                utime = int(parts[13])
                stime = int(parts[14])
        except Exception:
            return 0.0

        now = time.monotonic()
        wall_delta = now - self._prev_wall
        if wall_delta <= 0:
            return 0.0

        ticks = (utime - self._prev_utime) + (stime - self._prev_stime)
        cpu = (ticks / _CLK_TCK) / wall_delta * 100.0

        self._prev_utime = utime
        self._prev_stime = stime
        self._prev_wall = now
        return cpu
