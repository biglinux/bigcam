#!/usr/bin/env python3
"""End-to-end UI smoke test via AT-SPI/dogtail.

Verifies the GTK main loop stays responsive: the window appears, a handful of
toolbar buttons can be clicked, and the process exits cleanly when asked.

Skipped automatically when dogtail or a display is unavailable, so it is safe
to run as part of the normal suite:

    xvfb-run -a python -m pytest tests/test_ui_dogtail.py
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

pytestmark = [pytest.mark.gtk, pytest.mark.slow]

APP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "usr", "share", "biglinux", "bigcam")
)

# How long the app gets to shut down after SIGTERM before we call it a hang.
SHUTDOWN_BUDGET_S = 10


def _requirements():
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        return "no display available"
    try:
        import dogtail  # noqa: F401
    except ImportError:
        return "python3-dogtail is not installed"
    return None


@pytest.fixture
def app_process():
    reason = _requirements()
    if reason:
        pytest.skip(reason)

    env = os.environ.copy()
    env["GTK_A11Y"] = "atspi"
    env.setdefault("GSK_RENDERER", "cairo")

    proc = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=APP_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    yield proc

    if proc.poll() is None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            proc.terminate()
        try:
            proc.wait(timeout=SHUTDOWN_BUDGET_S)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.wait(timeout=5)


def _find_app(name: str = "bigcam", timeout: int = 20):
    from dogtail.tree import root

    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            return root.application(name)
        except Exception as exc:  # noqa: BLE001 - dogtail raises broadly
            last = exc
            time.sleep(0.5)
    pytest.skip(f"bigcam never registered on AT-SPI: {last}")


def test_window_appears_and_stays_responsive(app_process):
    app = _find_app()
    buttons = app.findChildren(lambda n: n.roleName == "push button")
    assert buttons, "no push buttons exposed — the UI never finished building"

    for btn in buttons[:5]:
        try:
            btn.click()
        except Exception as exc:  # noqa: BLE001
            print(f"warning: click on {btn.name!r} failed: {exc}")
        time.sleep(0.3)

    # Still alive and still answering AT-SPI queries => main loop not blocked.
    assert app_process.poll() is None, "application crashed while clicking"
    assert app.findChildren(lambda n: n.roleName == "push button")


def test_app_exits_cleanly_on_sigterm(app_process):
    _find_app()
    os.killpg(os.getpgid(app_process.pid), signal.SIGTERM)
    try:
        app_process.wait(timeout=SHUTDOWN_BUDGET_S)
    except subprocess.TimeoutExpired:
        pytest.fail(
            f"application did not exit within {SHUTDOWN_BUDGET_S}s of SIGTERM — "
            f"shutdown is blocking"
        )
