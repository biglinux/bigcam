"""Audio monitor – detect and play audio from USB camera devices."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading

import gi

gi.require_version("Gst", "1.0")

from gi.repository import Gst, GLib, GObject

log = logging.getLogger(__name__)


def _get_usb_parent(sysfs_path: str) -> str | None:
    """Walk the real sysfs path and return the USB bus-port identifier."""
    try:
        real = os.path.realpath(sysfs_path)
    except OSError:
        return None
    for part in reversed(real.split("/")):
        if re.match(r"^\d+-\d+(\.\d+)*$", part):
            return part
    return None


def _video_label(dev_name: str) -> str:
    """Return a short label from the V4L2 device name."""
    try:
        raw = open(f"/sys/class/video4linux/{dev_name}/name").read().strip()
    except OSError:
        return dev_name
    # Remove USB VID/PID suffixes like "(345f:2109): USB Vid"
    raw = re.sub(r"\s*\([\da-fA-F]+:[\da-fA-F]+\).*", "", raw)
    # Remove ": <suffix>" from truncated sysfs names
    raw = re.sub(r":\s+\S{1,3}$", "", raw)
    # Trim vendor prefix like "Microsoft® "
    raw = re.sub(r"^[^\s]+®\s+", "", raw)
    return raw.strip() or dev_name


def find_all_audio_sources() -> list[tuple[str, str]]:
    """Return ``[(pulse_source_name, display_label), ...]`` for all USB cameras with audio."""
    # Map USB parent → list of (card_num)
    usb_to_cards: dict[str, list[str]] = {}
    try:
        for entry in os.listdir("/sys/class/sound/"):
            if not entry.startswith("card"):
                continue
            card_usb = _get_usb_parent(f"/sys/class/sound/{entry}")
            if card_usb:
                card_num = entry.replace("card", "")
                usb_to_cards.setdefault(card_usb, []).append(card_num)
    except OSError:
        return []

    if not usb_to_cards:
        return []

    # Map USB parent → video device label (only first video node per USB parent)
    usb_to_label: dict[str, str] = {}
    try:
        for entry in sorted(os.listdir("/sys/class/video4linux/")):
            if not entry.startswith("video"):
                continue
            vid_usb = _get_usb_parent(f"/sys/class/video4linux/{entry}")
            if vid_usb and vid_usb in usb_to_cards and vid_usb not in usb_to_label:
                usb_to_label[vid_usb] = _video_label(entry)
    except OSError:
        pass

    # Query PulseAudio/PipeWire sources
    try:
        result = subprocess.run(
            ["pactl", "list", "sources"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0:
        return []

    # Collect all matching cards across USB parents
    all_cards: dict[str, str] = {}  # card_num → usb_parent
    for usb_parent, cards in usb_to_cards.items():
        if usb_parent in usb_to_label:
            for c in cards:
                all_cards[c] = usb_parent

    sources: list[tuple[str, str]] = []
    cur_name: str | None = None
    cur_card: str | None = None
    is_monitor = False

    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Name:"):
            cur_name = stripped.split(":", 1)[1].strip()
            cur_card = None
            is_monitor = ".monitor" in cur_name
        elif stripped.startswith("alsa.card ="):
            val = stripped.split("=", 1)[1].strip().strip('"')
            cur_card = val

        if cur_name and cur_card and not is_monitor:
            if cur_card in all_cards:
                usb = all_cards[cur_card]
                label = usb_to_label.get(usb, cur_name)
                sources.append((cur_name, label))
                del all_cards[cur_card]
            cur_name = cur_card = None

    return sources


class AudioMonitor(GObject.Object):
    """Manages multiple audio sources from USB camera devices.

    Signals
    -------
    sources-changed
        The list of available sources changed.  Use :pyattr:`sources`.
    volume-changed(float)
        The volume changed (0.0 – 1.0).
    mute-changed(bool)
        The mute state changed.
    """

    __gsignals__ = {
        "sources-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
        "volume-changed": (GObject.SignalFlags.RUN_LAST, None, (float,)),
        "mute-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
        "source-toggled": (GObject.SignalFlags.RUN_LAST, None, (str, bool)),
        "source-volume-changed": (GObject.SignalFlags.RUN_LAST, None, (str, float)),
    }

    def __init__(self) -> None:
        super().__init__()
        self._sources: list[tuple[str, str]] = []  # [(pulse_name, label)]
        self._pipelines: dict[str, Gst.Pipeline] = {}
        self._volume_elements: dict[str, Gst.Element] = {}
        self._source_volumes: dict[str, float] = {}  # per-source volume
        self._volume: float = 0.5
        self._muted: bool = False

    # -- public API ----------------------------------------------------------

    @property
    def sources(self) -> list[tuple[str, str]]:
        """Available audio sources as ``[(pulse_name, label), ...]``."""
        return list(self._sources)

    @property
    def volume(self) -> float:
        return self._volume

    @property
    def muted(self) -> bool:
        return self._muted

    def detect_all(self) -> None:
        """Detect all USB camera audio sources in a background thread."""
        threading.Thread(target=self._detect_worker, daemon=True).start()

    def is_active(self, source_name: str) -> bool:
        return source_name in self._pipelines

    @property
    def active_source_names(self) -> list[str]:
        """Return PulseAudio device names of all currently active sources."""
        return list(self._pipelines.keys())

    @property
    def all_source_names(self) -> list[str]:
        """Return PulseAudio device names of all detected sources."""
        return [s[0] for s in self._sources]

    def toggle_source(self, source_name: str) -> None:
        """Start or stop playback of a given source."""
        if source_name in self._pipelines:
            self._stop_source(source_name)
            self.emit("source-toggled", source_name, False)
        else:
            self._start_source(source_name)
            self.emit("source-toggled", source_name, True)

    def stop_all(self) -> None:
        """Stop all active pipelines."""
        for name in list(self._pipelines):
            self._stop_source(name)

    def set_volume(self, value: float) -> None:
        self._volume = max(0.0, min(1.0, value))
        for src, vol in self._volume_elements.items():
            vol.set_property("volume", self._volume)
            self._source_volumes[src] = self._volume
        self.emit("volume-changed", self._volume)

    def set_source_volume(self, source_name: str, value: float) -> None:
        """Set volume for a specific source."""
        value = max(0.0, min(1.0, value))
        self._source_volumes[source_name] = value
        vol_elem = self._volume_elements.get(source_name)
        if vol_elem:
            vol_elem.set_property("volume", value)
        self.emit("source-volume-changed", source_name, value)

    def get_source_volume(self, source_name: str) -> float:
        """Get volume for a specific source (defaults to global volume)."""
        return self._source_volumes.get(source_name, self._volume)

    def set_muted(self, muted: bool) -> None:
        self._muted = muted
        for vol in self._volume_elements.values():
            vol.set_property("mute", muted)
        self.emit("mute-changed", muted)

    def toggle_mute(self) -> None:
        self.set_muted(not self._muted)

    # -- internal ------------------------------------------------------------

    def _detect_worker(self) -> None:
        sources = find_all_audio_sources()
        GLib.idle_add(self._on_detected, sources)

    def _on_detected(self, sources: list[tuple[str, str]]) -> bool:
        new_names = {s[0] for s in sources}
        # Stop pipelines for sources that no longer exist
        for old_name in list(self._pipelines):
            if old_name not in new_names:
                log.info("Audio source removed: %s", old_name)
                self._stop_source(old_name)
        self._sources = sources
        log.info("Audio sources detected: %s", [s[1] for s in sources])
        self.emit("sources-changed")
        return GLib.SOURCE_REMOVE

    def _start_source(self, source: str) -> None:
        if source in self._pipelines:
            return
        pipeline_str = (
            f'pulsesrc device="{source}" '
            "do-timestamp=true "
            "buffer-time=200000 latency-time=50000 ! "
            "audioconvert ! "
            "audioresample ! "
            f"volume name=vol_{hash(source) & 0xFFFF:04x} ! "
            "queue max-size-time=1000000000 leaky=downstream ! "
            "autoaudiosink sync=false"
        )
        vol_name = f"vol_{hash(source) & 0xFFFF:04x}"
        try:
            pipeline = Gst.parse_launch(pipeline_str)
        except GLib.Error as exc:
            log.error("Failed to create audio pipeline for %s: %s", source, exc)
            return

        vol_elem = pipeline.get_by_name(vol_name)
        if vol_elem:
            src_vol = self._source_volumes.get(source, self._volume)
            vol_elem.set_property("volume", src_vol)
            vol_elem.set_property("mute", self._muted)

        bus = pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message::error", self._on_bus_error, source)
        bus.connect("message::eos", self._on_bus_eos, source)

        pipeline.set_state(Gst.State.PLAYING)
        self._pipelines[source] = pipeline
        if vol_elem:
            self._volume_elements[source] = vol_elem

    def _stop_source(self, source: str) -> None:
        pipeline = self._pipelines.pop(source, None)
        self._volume_elements.pop(source, None)
        if pipeline:
            pipeline.set_state(Gst.State.NULL)

    def _on_bus_eos(
        self, _bus: Gst.Bus, _msg: Gst.Message, source: str
    ) -> None:
        log.warning("Audio pipeline EOS for %s – restarting", source)
        GLib.timeout_add(500, self._restart_source, source)

    def _on_bus_error(
        self, _bus: Gst.Bus, msg: Gst.Message, source: str
    ) -> None:
        err, debug = msg.parse_error()
        log.error("Audio pipeline error for %s: %s (%s)", source, err.message, debug)
        # Restart the source; if device is gone, re-detect will clean up
        GLib.timeout_add(500, self._restart_source, source)
        GLib.timeout_add(2000, self._schedule_redetect)

    def _schedule_redetect(self) -> bool:
        """Debounced re-detection to avoid multiple concurrent scans."""
        self.detect_all()
        return GLib.SOURCE_REMOVE

    def _restart_source(self, source: str) -> bool:
        if source not in self._pipelines:
            return GLib.SOURCE_REMOVE
        self._stop_source(source)
        self._start_source(source)
        return GLib.SOURCE_REMOVE
