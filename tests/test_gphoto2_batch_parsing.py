"""Pairing gphoto2's batched --get-config output back to config paths.

Reading one control per subprocess is slow, so BigCam asks for up to 50 in a
single call.  The reply then has to be split back up and matched to the paths
that were requested — and getting that wrong is not cosmetic: the resulting
CameraControl.id is what set_control() later writes to, so a mis-pairing
silently changes the *wrong* setting on the camera.

The original implementation zipped blocks and paths by index, which only holds
while gphoto2 answers every request in order.  One skipped or failed config
shifts every control after it.
"""

from __future__ import annotations

from core.backends.gphoto2_backend import GPhoto2Backend

PATHS = [
    "/main/imgsettings/iso",
    "/main/capturesettings/shutterspeed",
    "/main/imgsettings/whitebalance",
]

# gphoto2 prints the config path before each block when several are requested.
WITH_PATHS = """\
/main/imgsettings/iso
Label: ISO Speed
Readonly: 0
Type: RADIO
Current: 400
Choice: 0 100
Choice: 1 400
END
/main/capturesettings/shutterspeed
Label: Shutter Speed
Readonly: 0
Type: RADIO
Current: 1/125
Choice: 0 1/60
Choice: 1 1/125
END
/main/imgsettings/whitebalance
Label: WhiteBalance
Readonly: 0
Type: RADIO
Current: Auto
Choice: 0 Auto
Choice: 1 Daylight
END
"""

# Same, but the middle config failed and produced no block at all.
WITH_PATHS_ONE_MISSING = """\
/main/imgsettings/iso
Label: ISO Speed
Readonly: 0
Type: RADIO
Current: 400
END
/main/imgsettings/whitebalance
Label: WhiteBalance
Readonly: 0
Type: RADIO
Current: Auto
END
"""

# Older gphoto2 builds emit no path header.
WITHOUT_PATHS = """\
Label: ISO Speed
Readonly: 0
Type: RADIO
Current: 400
END
Label: Shutter Speed
Readonly: 0
Type: RADIO
Current: 1/125
END
Label: WhiteBalance
Readonly: 0
Type: RADIO
Current: Auto
END
"""

WITHOUT_PATHS_ONE_MISSING = """\
Label: ISO Speed
Readonly: 0
Type: RADIO
Current: 400
END
Label: WhiteBalance
Readonly: 0
Type: RADIO
Current: Auto
END
"""


def _by_id(controls):
    return {c.id: c.name for c in controls}


# -- the reliable case: gphoto2 tells us the paths ------------------------


def test_paths_in_the_output_are_used():
    got = _by_id(GPhoto2Backend._parse_batch_output(PATHS, WITH_PATHS))
    assert got == {
        "/main/imgsettings/iso": "ISO Speed",
        "/main/capturesettings/shutterspeed": "Shutter Speed",
        "/main/imgsettings/whitebalance": "WhiteBalance",
    }


def test_a_missing_block_does_not_shift_the_others():
    """The regression: whitebalance used to be filed under shutterspeed."""
    got = _by_id(GPhoto2Backend._parse_batch_output(PATHS, WITH_PATHS_ONE_MISSING))
    assert got == {
        "/main/imgsettings/iso": "ISO Speed",
        "/main/imgsettings/whitebalance": "WhiteBalance",
    }
    assert "/main/capturesettings/shutterspeed" not in got


def test_output_order_does_not_matter():
    reordered = "\n".join(
        [
            "/main/imgsettings/whitebalance",
            "Label: WhiteBalance", "Readonly: 0", "Type: RADIO", "Current: Auto", "END",
            "/main/imgsettings/iso",
            "Label: ISO Speed", "Readonly: 0", "Type: RADIO", "Current: 400", "END",
        ]
    )
    got = _by_id(GPhoto2Backend._parse_batch_output(PATHS, reordered))
    assert got["/main/imgsettings/iso"] == "ISO Speed"
    assert got["/main/imgsettings/whitebalance"] == "WhiteBalance"


def test_a_path_we_did_not_ask_for_is_ignored():
    rogue = "/main/other/thing\nLabel: Rogue\nReadonly: 0\nType: TEXT\nCurrent: x\nEND\n"
    got = _by_id(GPhoto2Backend._parse_batch_output(PATHS, WITH_PATHS + rogue))
    assert "/main/other/thing" not in got


# -- the fallback case: no paths in the output ----------------------------


def test_positional_pairing_when_counts_match():
    got = _by_id(GPhoto2Backend._parse_batch_output(PATHS, WITHOUT_PATHS))
    assert got == {
        "/main/imgsettings/iso": "ISO Speed",
        "/main/capturesettings/shutterspeed": "Shutter Speed",
        "/main/imgsettings/whitebalance": "WhiteBalance",
    }


def test_count_mismatch_refuses_to_guess():
    """Rather than mis-file controls, return nothing so the caller retries."""
    got = GPhoto2Backend._parse_batch_output(PATHS, WITHOUT_PATHS_ONE_MISSING)
    assert got == [], "guessed a pairing it could not verify"


# -- degenerate input ------------------------------------------------------


def test_empty_output():
    assert GPhoto2Backend._parse_batch_output(PATHS, "") == []


def test_no_paths_requested():
    assert GPhoto2Backend._parse_batch_output([], WITH_PATHS) == []


def test_garbage_output_is_survivable():
    assert GPhoto2Backend._parse_batch_output(PATHS, "not gphoto2 output") == []
