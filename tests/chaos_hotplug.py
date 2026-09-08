#!/usr/bin/env python3
"""Safe chaos entry point: isolated regression tests, NEVER random /dev removals.

Physical unplug/replug is a separate opt-in manual test; passing this runner is
not proof of hardware hotplug correctness.
"""
from pathlib import Path
import subprocess
import sys

if __name__=="__main__":
    raise SystemExit(subprocess.call([sys.executable,"-m","pytest","-q",str(Path(__file__).parent/"audit")]))
