"""Every default setting must be read by something.

A key in the defaults that nothing ever reads is worse than a missing
feature: it looks like a supported option, so someone sets it and waits for
an effect that never comes.  Seven had accumulated —

    photo-directory, photo-format, photo-name-pattern, gphoto2-bitrate,
    auto-start-preview, sidebar-position, and the window geometry trio

— of which the window size was a feature worth having (the window reverted
to a hardcoded 1000x650 on every launch), and the rest were promises the
code never kept.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "usr/share/biglinux/bigcam"
DEFAULTS_FILE = APP / "utils/settings_manager.py"


def _declared_keys() -> list[str]:
    text = DEFAULTS_FILE.read_text()
    block = re.search(r"DEFAULTS[^=]*=\s*\{(.*?)\n\}", text, re.S)
    assert block, "could not find the DEFAULTS mapping"
    return re.findall(r'^\s*"([^"]+)":', block.group(1), re.M)


def _uses(key: str) -> int:
    """How many times *key* appears in the app, outside its declaration."""
    res = subprocess.run(
        ["grep", "-rF", f'"{key}"', "--include=*.py", str(APP)],
        capture_output=True, text=True,
    )
    lines = [
        line for line in res.stdout.splitlines()
        if line and "utils/settings_manager.py" not in line
    ]
    return len(lines)


def test_keys_were_found():
    keys = _declared_keys()
    assert len(keys) > 10, f"only parsed {len(keys)} keys; the regex is wrong"


@pytest.mark.parametrize("key", _declared_keys())
def test_the_setting_is_read_somewhere(key):
    assert _uses(key) > 0, (
        f"'{key}' is declared with a default and never read — it looks like "
        f"a supported option but does nothing"
    )


def test_the_removed_keys_are_gone():
    declared = set(_declared_keys())
    for key in (
        "photo-directory", "photo-format", "photo-name-pattern",
        "gphoto2-bitrate", "auto-start-preview", "sidebar-position",
    ):
        assert key not in declared, f"'{key}' came back without an implementation"


# -- window geometry, which is now real ------------------------------------


@pytest.mark.parametrize("key", ["window-width", "window-height", "window-maximized"])
def test_geometry_keys_are_still_declared(key):
    assert key in _declared_keys()


def _window_source() -> str:
    return (APP / "ui/window.py").read_text()


def test_geometry_is_restored_at_startup():
    src = _window_source()
    assert "_restore_geometry" in src
    assert "self.set_default_size(width, height)" in src, (
        "the size is read but not applied"
    )


def test_geometry_is_saved_on_close():
    src = _window_source()
    close = src[src.index("def _on_close"):]
    assert "_save_geometry" in close[:400], (
        "geometry is saved too late in the close path, or not at all"
    )


def test_a_nonsense_stored_size_is_not_applied():
    """A crash mid-resize can leave a 1x1 window stored."""
    import types

    from ui.window import BigDigicamWindow

    window = BigDigicamWindow.__new__(BigDigicamWindow)
    applied: list[tuple[int, int]] = []
    stored = {"window-width": 1, "window-height": 0, "window-maximized": False}

    window._settings = types.SimpleNamespace(get=stored.get)
    window.set_default_size = lambda w, h: applied.append((w, h))
    window.maximize = lambda: None

    BigDigicamWindow._restore_geometry(window)
    assert applied == [(1100, 700)], f"opened at {applied}"


def test_a_stored_size_is_applied():
    import types

    from ui.window import BigDigicamWindow

    window = BigDigicamWindow.__new__(BigDigicamWindow)
    applied: list[tuple[int, int]] = []
    stored = {"window-width": 1600, "window-height": 900, "window-maximized": False}

    window._settings = types.SimpleNamespace(get=stored.get)
    window.set_default_size = lambda w, h: applied.append((w, h))
    window.maximize = lambda: None

    BigDigicamWindow._restore_geometry(window)
    assert applied == [(1600, 900)]


def test_a_maximised_window_reopens_maximised():
    import types

    from ui.window import BigDigicamWindow

    window = BigDigicamWindow.__new__(BigDigicamWindow)
    maximised: list[bool] = []
    stored = {"window-width": 1600, "window-height": 900, "window-maximized": True}

    window._settings = types.SimpleNamespace(get=stored.get)
    window.set_default_size = lambda w, h: None
    window.maximize = lambda: maximised.append(True)

    BigDigicamWindow._restore_geometry(window)
    assert maximised == [True]


def test_the_maximised_frame_size_is_not_stored():
    """Storing the maximised size would make the restored window full-screen."""
    import types

    from ui.window import BigDigicamWindow

    window = BigDigicamWindow.__new__(BigDigicamWindow)
    written: dict[str, object] = {}

    window._settings = types.SimpleNamespace(
        set=lambda k, v: written.__setitem__(k, v), get=lambda k, d=None: d
    )
    window.is_maximized = lambda: True
    window.get_default_size = lambda: (3840, 2160)

    BigDigicamWindow._save_geometry(window)
    assert written == {"window-maximized": True}, f"also stored {written}"
