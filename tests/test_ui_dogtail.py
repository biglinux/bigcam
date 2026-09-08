#!/usr/bin/env python3
"""Explicit graphical entry point; missing accessibility dependencies are failures."""
import os
from pathlib import Path
import subprocess
import sys

if __name__=="__main__":
    if not os.environ.get("BIGCAM_TEST_RESULTS"):
        raise SystemExit("Set BIGCAM_TEST_RESULTS and run as a normal user. This test creates a private display.")
    raise SystemExit(subprocess.call(["bash",str(Path(__file__).parent/"integration/session.sh")]))
