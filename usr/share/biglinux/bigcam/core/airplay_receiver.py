"""AirPlay receiver – iPhone/Mac screen mirroring via UxPlay."""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import threading
from typing import Optional

from gi.repository import GLib, GObject

log = logging.getLogger(__name__)

_UXPLAY_BIN = "uxplay"


class AirPlayReceiver(GObject.Object):
    """Manage a UxPlay subprocess for AirPlay mirroring to v4l2loopback.

    UxPlay receives AirPlay Mirror/Audio connections from iOS/macOS
    devices and outputs decoded video to a v4l2loopback device via
    GStreamer's v4l2sink.
    """

    __gsignals__ = {
        "connected": (GObject.SignalFlags.RUN_LAST, None, (int, int)),
        "disconnected": (GObject.SignalFlags.RUN_LAST, None, ()),
        "status-changed": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }

    def __init__(self) -> None:
        super().__init__()
        self._process: Optional[subprocess.Popen] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._v4l2_device: str = ""
        self._running = False

    # -- availability --------------------------------------------------------

    @staticmethod
    def is_available() -> bool:
        """Return True if the uxplay binary is on PATH."""
        return shutil.which(_UXPLAY_BIN) is not None

    @staticmethod
    def uxplay_version() -> str:
        """Return the UxPlay version string or empty on failure."""
        try:
            out = subprocess.run(
                [_UXPLAY_BIN, "-h"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            # UxPlay prints version in first lines of help output
            combined = out.stdout + out.stderr
            m = re.search(r"UxPlay\s+(\d+\.\d+(?:\.\d+)?)", combined)
            return m.group(1) if m else ""
        except Exception:
            return ""

    # -- properties ----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def pid(self) -> int | None:
        proc = self._process
        return proc.pid if proc else None

    @property
    def v4l2_device(self) -> str:
        return self._v4l2_device

    # -- start / stop --------------------------------------------------------

    def start(
        self,
        v4l2_device: str,
        server_name: str = "BigCam",
        max_size: int = 1080,
        fps: int = 30,
        rotation: str = "",
    ) -> bool:
        """Start UxPlay outputting to *v4l2_device*.

        *rotation* can be "R" (90° right), "L" (90° left), "I" (180°) or "".
        Returns True if the process started successfully.
        """
        if self._running:
            self.stop()

        self._v4l2_device = v4l2_device
        resolution = f"{max_size * 16 // 9}x{max_size}" if max_size else "1920x1080"

        cmd: list[str] = [
            "stdbuf", "-oL",
            _UXPLAY_BIN,
            "-n", server_name,
            "-nh",
            "-vs", f"v4l2sink device={v4l2_device}",
            "-s", resolution,
            "-fps", str(fps),
            "-vsync", "no",
        ]

        if rotation in ("R", "L"):
            cmd.extend(["-r", rotation])
        elif rotation == "I":
            cmd.extend(["-f", "I"])

        log.info("Starting UxPlay: %s", " ".join(cmd))
        self.emit("status-changed", "Starting AirPlay receiver...")

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except Exception as exc:
            log.error("Failed to start UxPlay: %s", exc)
            self.emit("status-changed", f"Error: {exc}")
            return False

        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_output, daemon=True
        )
        self._monitor_thread.start()
        return True

    def stop(self) -> None:
        """Stop the UxPlay process."""
        proc = self._process
        self._process = None
        self._running = False
        self._v4l2_device = ""
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()

    # -- internal monitoring -------------------------------------------------

    def _monitor_output(self) -> None:
        """Read UxPlay stdout/stderr and emit signals."""
        proc = self._process
        if not proc or not proc.stdout:
            return

        connected = False
        width, height = 0, 0

        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue

            log.debug("uxplay: %s", line)

            # Detect connection: "raop_rtp_mirror starting mirroring"
            if "starting mirroring" in line.lower() and not connected:
                connected = True
                GLib.idle_add(
                    self.emit, "status-changed", "AirPlay client connected"
                )
                GLib.idle_add(self.emit, "connected", width or 1920, height or 1080)

            # Detect resolution from "raop_rtp_mirror ... WxH" patterns
            res_match = re.search(r"(\d{3,4})x(\d{3,4})", line)
            if res_match:
                width = int(res_match.group(1))
                height = int(res_match.group(2))

            # Detect disconnection
            if "client disconnected" in line.lower() or "connection closed" in line.lower():
                if connected:
                    connected = False
                    GLib.idle_add(self.emit, "disconnected")
                    GLib.idle_add(
                        self.emit, "status-changed", "Client disconnected"
                    )

            # Log errors/warnings
            if "error" in line.lower():
                log.warning("uxplay error: %s", line)
                GLib.idle_add(self.emit, "status-changed", line)

        # Process exited
        log.info("UxPlay process exited")
        self._running = False
        if connected:
            GLib.idle_add(self.emit, "disconnected")
        GLib.idle_add(self.emit, "status-changed", "AirPlay receiver stopped")
