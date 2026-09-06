"""Reading 1-D barcodes across OpenCV's two return shapes.

The barcode fallback has been broken twice for the same reason: the call site
was wrong and the caller wrapped it in `except Exception: pass`, so nothing
ever surfaced.  First it imported the `zbar` module, which only ships a
Python 2 binding and could never import.  Then it unpacked
BarcodeDetector.detectAndDecode into four names — the OpenCV 4.x shape — on a
system with OpenCV 5.x, where it returns three.

    4.x: (ok, decoded_info, decoded_type, points)
    5.x: (decoded_text, points, straight_code)

Neither the build container's OpenCV version nor the user's is fixed, so both
shapes have to work.  These tests use fakes: they are about the unpacking,
and a real barcode image would only ever exercise whichever version is
installed here.
"""

from __future__ import annotations

import numpy as np
import pytest

from ui.settings_page import _decode_barcode

IMG = np.zeros((64, 64, 3), dtype=np.uint8)
PTS = np.array([[0, 0], [10, 0], [10, 10], [0, 10]], dtype=np.float32)


class _Detector:
    def __init__(self, result):
        self._result = result

    def detectAndDecode(self, _img):
        return self._result


# -- OpenCV 5.x: (text, points, straight_code) -----------------------------


def test_v5_hit():
    text, pts = _decode_barcode(_Detector(("9780306406157", PTS, None)), IMG)
    assert text == "9780306406157"
    assert pts is PTS


def test_v5_miss():
    assert _decode_barcode(_Detector(("", None, None)), IMG) == ("", None)


def test_v5_hit_without_points():
    text, pts = _decode_barcode(_Detector(("CODE128", None, None)), IMG)
    assert (text, pts) == ("CODE128", None)


# -- OpenCV 4.x: (ok, texts, formats, points) ------------------------------


def test_v4_hit():
    detector = _Detector((True, ["9780306406157"], ["EAN_13"], [PTS]))
    text, pts = _decode_barcode(detector, IMG)
    assert text == "9780306406157"
    assert pts is PTS


def test_v4_miss():
    assert _decode_barcode(_Detector((False, [], [], None)), IMG) == ("", None)


def test_v4_skips_empty_hits():
    """A detected-but-undecodable code comes back as an empty string."""
    detector = _Detector((True, ["", "CODE39"], ["", "CODE_39"], [PTS, PTS]))
    text, _pts = _decode_barcode(detector, IMG)
    assert text == "CODE39"


def test_v4_all_hits_empty():
    assert _decode_barcode(_Detector((True, [""], [""], [PTS])), IMG) == ("", None)


def test_v4_hit_with_missing_points():
    detector = _Detector((True, ["EAN8"], ["EAN_8"], None))
    assert _decode_barcode(detector, IMG) == ("EAN8", None)


# -- the real detector, whichever version is installed ---------------------


def test_the_installed_opencv_is_one_of_the_two_shapes():
    cv2 = pytest.importorskip("cv2")
    if not hasattr(cv2, "barcode"):
        pytest.skip("OpenCV built without the barcode module")
    detector = cv2.barcode.BarcodeDetector()
    # A blank frame decodes to nothing, but must not raise on unpacking.
    assert _decode_barcode(detector, IMG) == ("", None)
