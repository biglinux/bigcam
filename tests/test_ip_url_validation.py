"""IP camera URLs are pasted straight into a GStreamer pipeline description.

`gst_parse_launch` takes a whole pipeline as one string, so a URL that closes
the quote around `location=` can append arbitrary elements to it — a filesink
that overwrites a file, or a source that reads one.  The URL comes from a text
entry and is persisted to settings, which are also editable by hand, so the
dialog's scheme check is not the only path in.

Anything that cannot be proved safe is rejected: no pipeline is better than
someone else's pipeline.
"""

from __future__ import annotations

import pytest

from constants import BackendType
from core.backends.ip_backend import IPBackend, validate_stream_url
from core.camera_backend import CameraInfo

GOOD = [
    "rtsp://192.168.0.10:554/stream1",
    "rtsps://cam.local/live",
    "http://10.0.0.5:8080/video",
    "https://example.com/mjpeg?res=hd&fps=30",
    "rtsp://user:pass@192.168.0.10/h264",
    "http://cam/path%20with%20escapes",
    "rtsp://[2001:db8::1]:554/stream",
]

BAD = [
    # quote breakout: everything after the quote becomes pipeline syntax
    'http://x/" ! filesink location="/home/user/.bashrc',
    "rtsp://x/' ! fakesink",
    # a bare ! is an element separator
    "http://x/a ! fakesink",
    # backslash is the escape character inside a quoted gst string
    "http://x/\\",
    # whitespace splits tokens
    "http://x /y",
    "http://x\n! fakesink",
    "http://x\t! fakesink",
    # schemes that reach the local filesystem or spawn a process
    "file:///etc/shadow",
    "/etc/passwd",
    "fdsrc://0",
    "",
    "   ",
]


@pytest.mark.parametrize("url", GOOD)
def test_ordinary_camera_urls_are_accepted(url):
    assert validate_stream_url(url) == url


@pytest.mark.parametrize("url", BAD)
def test_dangerous_urls_are_rejected(url):
    assert validate_stream_url(url) == "", f"accepted {url!r}"


def test_scheme_is_case_insensitive():
    assert validate_stream_url("RTSP://cam/live") == "RTSP://cam/live"


def test_surrounding_whitespace_is_trimmed():
    assert validate_stream_url("  rtsp://cam/live  ") == "rtsp://cam/live"


# -- the pipeline the backend actually builds ------------------------------


def _camera(url: str) -> CameraInfo:
    return CameraInfo(
        id=f"ip:{url}", name="Cam", backend=BackendType.IP,
        device_path=url, extra={"url": url},
    )


def test_rtsp_source_uses_rtspsrc():
    src = IPBackend().get_gst_source(_camera("rtsp://cam/live"))
    assert src.startswith("rtspsrc ")
    assert "rtsp://cam/live" in src


def test_http_source_uses_souphttpsrc():
    assert IPBackend().get_gst_source(_camera("http://cam/v")).startswith("souphttpsrc ")


@pytest.mark.parametrize("url", BAD)
def test_no_pipeline_is_built_for_a_rejected_url(url):
    assert IPBackend().get_gst_source(_camera(url)) == ""


def test_photo_capture_refuses_a_rejected_url(monkeypatch):
    import core.backends.ip_backend as mod

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("spawned gst-launch for a rejected URL")

    monkeypatch.setattr(mod.subprocess, "run", _boom)
    assert IPBackend().capture_photo(_camera('http://x/" ! fakesink'), "/tmp/x.jpg") is False


# -- cameras_from_urls is the entry point for persisted settings -----------


def test_saved_entries_with_bad_urls_are_dropped():
    cams = IPBackend().cameras_from_urls(
        [
            {"name": "good", "url": "rtsp://cam/live"},
            {"name": "evil", "url": 'http://x/" ! filesink location="/tmp/pwn'},
        ]
    )
    assert [c.name for c in cams] == ["good"]
