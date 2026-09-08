"""Own the two DSLR producer processes; never locate/kill processes by name."""
from __future__ import annotations

import re
import signal
import subprocess
import sys
import tempfile
import threading
import time


def stop_process(process: subprocess.Popen | None) -> None:
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    else:
        process.wait()


class GPhotoSession:
    """gphoto2 stdout -> FFmpeg stdin -> localhost MPEG-TS, with explicit ownership."""
    def __init__(self, port: str, udp_port: int, bitrate: int = 5000):
        if not re.fullmatch(r"usb:[0-9]{1,3},[0-9]{1,3}", port):
            raise ValueError("A specific USB port is required")
        if not 1024 <= int(udp_port) <= 65535:
            raise ValueError("Invalid UDP port")
        self.port, self.udp_port = port, int(udp_port)
        self.bitrate = max(500, min(50000, int(bitrate)))
        self.camera_process = None
        self.encoder_process = None
        self._diagnostics = None
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        return all(p is not None and p.poll() is None for p in (self.camera_process, self.encoder_process))

    def start(self) -> bool:
        with self._lock:
            if self.running:
                return True
            self.stop()
            self._diagnostics = tempfile.TemporaryFile()
            try:
                self.camera_process = subprocess.Popen(
                    ["gphoto2", "--port", self.port, "--stdout", "--capture-movie"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=self._diagnostics,
                    start_new_session=True)
                self.encoder_process = subprocess.Popen(
                    ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
                     "-an", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
                     "-f", "mpegts", "-r", "30", "-codec:v", "mpeg1video",
                     "-b:v", f"{self.bitrate}k", "-bf", "0",
                     f"udp://127.0.0.1:{self.udp_port}?pkt_size=1316"],
                    stdin=self.camera_process.stdout, stdout=subprocess.DEVNULL,
                    stderr=self._diagnostics, start_new_session=True)
                # Only FFmpeg owns the pipe reader after this point.
                self.camera_process.stdout.close()
                # Process startup is not a claim that a preview frame arrived.
                return self.running
            except (OSError, subprocess.SubprocessError):
                self.stop()
                raise

    def stop(self) -> None:
        with self._lock:
            try:
                stop_process(self.camera_process)
            finally:
                stop_process(self.encoder_process)
                self.camera_process = self.encoder_process = None
                if self._diagnostics:
                    self._diagnostics.close()
                    self._diagnostics = None


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("Usage: gphoto_session.py usb:BUS,DEVICE UDP_PORT")
    session = GPhotoSession(sys.argv[1], int(sys.argv[2]))
    stopped = threading.Event()
    for event in (signal.SIGINT, signal.SIGTERM):
        signal.signal(event, lambda *_: stopped.set())
    try:
        if not session.start():
            return 1
        while not stopped.wait(0.2):
            if not session.running:
                return 1
        return 0
    finally:
        session.stop()


if __name__ == "__main__":
    raise SystemExit(main())
