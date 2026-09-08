"""Frame-rate and device-format contracts shared by capture backends."""
from fractions import Fraction
import math


def frame_rate(value: float) -> str:
    value = float(value)
    if not math.isfinite(value) or value <= 0 or value > 1000:
        raise ValueError("Invalid frame rate")
    # v4l2-ctl displays rounded decimals for NTSC frame intervals.
    for numerator in (24000, 30000, 60000, 120000):
        if abs(value - numerator / 1001) < 0.005:
            return f"{numerator}/1001"
    rate = Fraction(str(value)).limit_denominator(100000)
    return f"{rate.numerator}/{rate.denominator}"


def source_caps(fmt) -> tuple[str, str]:
    """Return caps and decoder for a supported V4L2 FOURCC, without guessing raw."""
    compressed = {"MJPG": ("image/jpeg", "jpegdec"), "JPEG": ("image/jpeg", "jpegdec"),
                  "H264": ("video/x-h264", "h264parse ! decodebin"),
                  "HEVC": ("video/x-h265", "h265parse ! decodebin"),
                  "H265": ("video/x-h265", "h265parse ! decodebin")}
    raw = {"YUYV": "YUY2", "YUY2": "YUY2", "UYVY": "UYVY", "NV12": "NV12",
           "NV21": "NV21", "YU12": "I420", "YV12": "YV12", "RGB3": "RGB",
           "BGR3": "BGR", "GREY": "GRAY8", "RGBx": "RGBx", "BGRx": "BGRx"}
    if fmt.pixel_format in compressed:
        caps, decoder = compressed[fmt.pixel_format]
    elif fmt.pixel_format in raw:
        caps, decoder = "video/x-raw,format=" + raw[fmt.pixel_format], ""
    else:
        raise ValueError(f"Unsupported camera pixel format: {fmt.pixel_format}")
    if fmt.width <= 0 or fmt.height <= 0:
        raise ValueError("Invalid frame dimensions")
    caps += f",width={fmt.width},height={fmt.height}"
    if fmt.fps:
        caps += ",framerate=" + frame_rate(max(fmt.fps))
    return caps, decoder
