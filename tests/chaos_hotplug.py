#!/usr/bin/env python3
"""Chaos Hotplug — aggressive v4l2loopback add/remove to stress the hotplug path.

Goal: make sure CameraManager's debounce/polling and the EventBus never
dead-lock or leak devices when devices appear and disappear rapidly.

Requires passwordless sudo for ``v4l2loopback-ctl`` (see etc/sudoers.d/bigcam)
and the v4l2loopback module loaded.  Run manually:

    python3 tests/chaos_hotplug.py --seconds 30
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import shutil
import subprocess
import sys
import threading
import time

# Ensure bigcam modules are importable
sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../usr/share/biglinux/bigcam")
    ),
)

from utils.command_runner import SecureCommandRunner  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("ChaosMonkey")

_CTL = shutil.which("v4l2loopback-ctl") or "/usr/sbin/v4l2loopback-ctl"

# Chaos devices live well above both the static pool (20-24) and the
# dynamic range BigCam allocates, so a crash never eats a real device.
_CHAOS_MIN, _CHAOS_MAX = 50, 99


class ChaosHotplugger:
    """Randomly creates and destroys v4l2loopback devices in a background thread."""

    def __init__(self, num_devices: int = 3) -> None:
        self.num_devices = num_devices
        self.active_devices: list[str] = []
        self.running = False
        self.thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self.running = True
        self.thread = threading.Thread(target=self._chaos_loop, daemon=True)
        self.thread.start()
        log.info("Chaos Monkey started.")

    def stop(self) -> None:
        self.running = False
        if self.thread is not None:
            self.thread.join(timeout=10)
            self.thread = None
        with self._lock:
            pending = list(self.active_devices)
        for dev in pending:
            self._remove_device(dev)
        log.info("Chaos Monkey finished. Leaked devices: %s", self.active_devices)

    # -- device management -------------------------------------------------

    def _add_device(self) -> str:
        dev_num = random.randint(_CHAOS_MIN, _CHAOS_MAX)
        dev_path = f"/dev/video{dev_num}"
        with self._lock:
            if dev_path in self.active_devices:
                return ""
        if os.path.exists(dev_path):
            return ""

        log.info("Adding chaos device: %s", dev_path)
        try:
            result = SecureCommandRunner.run_safe(
                ["sudo", "-n", _CTL, "add", "-n", f"ChaosCam {dev_num}", dev_path],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("add failed: %s", exc)
            return ""
        if result.returncode != 0:
            log.warning("add rc=%d: %s", result.returncode, result.stderr.strip())
            return ""
        with self._lock:
            self.active_devices.append(dev_path)
        return dev_path

    def _remove_device(self, dev_path: str) -> None:
        with self._lock:
            if dev_path not in self.active_devices:
                return
        log.info("Removing chaos device: %s", dev_path)
        try:
            result = SecureCommandRunner.run_safe(
                ["sudo", "-n", _CTL, "delete", dev_path],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("delete failed: %s", exc)
            return
        if result.returncode != 0:
            log.warning("delete rc=%d: %s", result.returncode, result.stderr.strip())
            return
        with self._lock:
            if dev_path in self.active_devices:
                self.active_devices.remove(dev_path)

    def _chaos_loop(self) -> None:
        while self.running:
            action = random.choice(["add", "remove", "add", "add"])
            with self._lock:
                count = len(self.active_devices)
                snapshot = list(self.active_devices)
            if action == "add" and count < self.num_devices:
                self._add_device()
            elif action == "remove" and snapshot:
                self._remove_device(random.choice(snapshot))
            time.sleep(random.uniform(0.1, 1.5))


def _preflight() -> bool:
    """Return True when the environment can actually run the chaos test."""
    if not os.path.isfile(_CTL):
        log.error("v4l2loopback-ctl not found at %s", _CTL)
        return False
    if not os.path.isdir("/sys/module/v4l2loopback"):
        log.error("v4l2loopback module is not loaded.")
        return False
    try:
        probe = subprocess.run(
            ["sudo", "-n", "true"], capture_output=True, timeout=5
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if probe.returncode != 0:
        log.error("Passwordless sudo is not available.")
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--devices", type=int, default=5)
    args = parser.parse_args()

    if not _preflight():
        log.warning("Environment cannot run the chaos test — skipping.")
        return 0

    chaos = ChaosHotplugger(num_devices=args.devices)
    chaos.start()
    try:
        log.info("Running hotplug chaos for %d seconds...", args.seconds)
        time.sleep(args.seconds)
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    finally:
        chaos.stop()
    return 1 if chaos.active_devices else 0


if __name__ == "__main__":
    raise SystemExit(main())
