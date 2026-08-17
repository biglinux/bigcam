#!/usr/bin/env python3
"""
Chaos Hotplug - Simula conexão e desconexão agressiva de dispositivos V4L2.
Objetivo: Garantir que o EventBus e a UI do BigCam não sofram deadlock.
"""

import os
import sys
import time
import random
import threading
import logging
from typing import List

# Ensure bigcam modules are importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../usr/share/biglinux/bigcam")))

from utils.command_runner import SecureCommandRunner

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ChaosMonkey")

class ChaosHotplugger:
    def __init__(self, num_devices=3):
        self.num_devices = num_devices
        self.runner = SecureCommandRunner()
        self.active_devices: List[str] = []
        self.running = False
        self.thread = None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._chaos_loop, daemon=True)
        self.thread.start()
        log.info("Chaos Monkey iniciado.")

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join()
        # Clean up
        for dev in list(self.active_devices):
            self._remove_device(dev)
        log.info("Chaos Monkey finalizado.")

    def _add_device(self) -> str:
        # We rely on pkexec / sudo rules being set up for v4l2loopback-ctl
        dev_num = random.randint(50, 99)
        dev_path = f"/dev/video{dev_num}"
        if dev_path in self.active_devices:
            return ""
            
        log.info(f"Adding chaos device: {dev_path}")
        success, _, _ = self.runner.run_sync(
            ["sudo", "-n", "v4l2loopback-ctl", "add", "-n", f"ChaosCam {dev_num}", dev_path],
            timeout=5.0
        )
        if success:
            self.active_devices.append(dev_path)
            return dev_path
        return ""

    def _remove_device(self, dev_path: str):
        if dev_path in self.active_devices:
            log.info(f"Removing chaos device: {dev_path}")
            self.runner.run_sync(
                ["sudo", "-n", "v4l2loopback-ctl", "delete", dev_path],
                timeout=5.0
            )
            self.active_devices.remove(dev_path)

    def _chaos_loop(self):
        while self.running:
            action = random.choice(["add", "remove", "add", "add"])
            
            if action == "add" and len(self.active_devices) < self.num_devices:
                self._add_device()
            elif action == "remove" and self.active_devices:
                dev_to_remove = random.choice(self.active_devices)
                self._remove_device(dev_to_remove)
                
            time.sleep(random.uniform(0.1, 1.5))


if __name__ == "__main__":
    if os.geteuid() != 0 and not os.system("sudo -n true") == 0:
        log.error("This test requires sudo-nopasswd for v4l2loopback-ctl.")
        sys.exit(1)
        
    chaos = ChaosHotplugger(num_devices=5)
    chaos.start()
    
    try:
        log.info("Running hotplug chaos for 30 seconds...")
        time.sleep(30)
    except KeyboardInterrupt:
        log.info("Interrupted by user")
    finally:
        chaos.stop()
