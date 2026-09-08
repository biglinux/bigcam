"""Network camera URLs and GStreamer property quoting (not shell quoting)."""
from __future__ import annotations

import hashlib
from urllib.parse import urlsplit, urlunsplit

_ALLOWED = {"rtsp", "rtsps", "http", "https"}


def camera_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Invalid camera URL")
    parts = urlsplit(value.strip())
    if parts.scheme.lower() not in _ALLOWED or not parts.hostname:
        raise ValueError("Use an HTTP(S) or RTSP(S) URL with a host")
    # Access validates malformed and out-of-range ports.
    _ = parts.port
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, parts.query, ""))


def public_camera_name(value: str) -> str:
    parts = urlsplit(camera_url(value))
    # Deliberately omit user info, path, query and fragment; these may contain secrets.
    return f"{parts.scheme}://{parts.hostname}"


def camera_url_id(value: str) -> str:
    return "ip:" + hashlib.sha256(camera_url(value).encode("utf-8")).hexdigest()


def gst_quote(value: str) -> str:
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise ValueError("Invalid GStreamer property string")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
