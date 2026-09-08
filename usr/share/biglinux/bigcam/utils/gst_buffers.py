"""Create owned Gst video buffers with explicit packed BGR row layout."""
import numpy as np
import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import Gst, GstVideo


def bgr_buffer(frame):
    frame = np.ascontiguousarray(frame, dtype=np.uint8)
    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("Expected a three-channel BGR image")
    height, width = frame.shape[:2]
    if not height or not width:
        raise ValueError("Empty BGR image")
    buffer = Gst.Buffer.new_wrapped(frame.tobytes())
    GstVideo.buffer_add_video_meta_full(buffer, GstVideo.VideoFrameFlags.NONE,
                                       GstVideo.VideoFormat.BGR, width, height, 1,
                                       [0, 0, 0, 0], [width * 3, 0, 0, 0])
    return buffer
