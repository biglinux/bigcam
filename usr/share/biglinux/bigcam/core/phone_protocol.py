"""Pure input limits and authenticated WebTransport stream assembly."""
from collections import OrderedDict
import io
import secrets
import time
from PIL import Image

MAX_FRAME_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 16 * 1024 * 1024


def valid_token(value, expected):
    return isinstance(value, str) and value.isascii() and 0 < len(value) <= 128 and secrets.compare_digest(value, expected)


def jpeg_size(data):
    if not data or len(data) > MAX_FRAME_BYTES or not data.startswith(b"\xff\xd8"):
        raise ValueError("Invalid camera JPEG")
    with Image.open(io.BytesIO(data)) as image:
        w, h = image.size
        if image.format != "JPEG" or w <= 0 or h <= 0 or max(w, h) > 8192 or w * h > MAX_PIXELS:
            raise ValueError("Camera image exceeds the decoding limit")
        return w, h


class StreamAssembler:
    """One authorized CONNECT session, finite streams/bytes and no stale fragments."""
    def __init__(self, session_id, clock=time.monotonic):
        self.session_id = session_id
        self.clock = clock
        self.streams = OrderedDict()
        self.total = 0

    def discard(self, stream_id):
        entry = self.streams.pop(stream_id, None)
        if entry:
            self.total -= len(entry[1])

    def feed(self, session_id, stream_id, data, ended):
        if session_id != self.session_id or self.session_id is None:
            raise PermissionError("Stream does not belong to the authenticated session")
        now = self.clock()
        if any(now - created > 3 for created, _buf in self.streams.values()):
            raise ValueError("Unfinished camera stream expired")
        if stream_id not in self.streams:
            if len(self.streams) >= 8:
                raise ValueError("Too many concurrent camera streams")
            self.streams[stream_id] = (now, bytearray())
        _created, buf = self.streams[stream_id]
        if len(buf) + len(data) > MAX_FRAME_BYTES or self.total + len(data) > 2 * MAX_FRAME_BYTES:
            raise ValueError("Camera stream byte budget exceeded")
        buf.extend(data)
        self.total += len(data)
        if ended:
            result = bytes(buf)
            self.discard(stream_id)
            return result
        return None
