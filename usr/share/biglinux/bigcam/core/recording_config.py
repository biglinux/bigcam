"""Validated, immutable recording options and encoder fallbacks."""
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class RecordingConfig:
    video_codec: str = "h264"
    audio_codec: str = "opus"
    container: str = "mkv"
    video_bitrate: int = 8000

    def __post_init__(self):
        if self.video_codec not in {"h264", "h265", "vp9", "mjpeg"}:
            raise ValueError("Unknown video codec")
        if self.audio_codec not in {"opus", "aac", "mp3", "vorbis"}:
            raise ValueError("Unknown audio codec")
        if self.container not in {"mkv", "mp4", "webm"}:
            raise ValueError("Unknown container")
        if isinstance(self.video_bitrate, bool) or not math.isfinite(float(self.video_bitrate)):
            raise ValueError("Invalid video bitrate")
        object.__setattr__(self, "video_bitrate", max(500, min(50000, int(self.video_bitrate))))
        if self.container == "webm":
            object.__setattr__(self, "video_codec", "vp9")
            if self.audio_codec not in {"opus", "vorbis"}:
                object.__setattr__(self, "audio_codec", "opus")
        if self.container == "mp4":
            # mp4mux supports VP9 and Opus, but not JPEG or Vorbis.
            if self.video_codec == "mjpeg":
                object.__setattr__(self, "video_codec", "h264")
            if self.audio_codec == "vorbis":
                object.__setattr__(self, "audio_codec", "aac")

    @property
    def extension(self):
        return "." + self.container

    @property
    def muxer(self):
        return {"mkv": "matroskamux", "mp4": "mp4mux", "webm": "webmmux"}[self.container]

    def encoders(self):
        """Ordered candidates. Each must actually encode before it is selected."""
        br = self.video_bitrate
        if self.video_codec == "h265":
            return [("nvh265enc", f"nvh265enc bitrate={br} rc-mode=cbr ! h265parse"),
                    ("vah265enc", f"vah265enc bitrate={br} rate-control=cbr ! h265parse"),
                    ("x265enc", f"x265enc bitrate={br} speed-preset=veryfast tune=zerolatency ! h265parse")]
        if self.video_codec == "vp9":
            return [("vavp9enc", f"vavp9enc bitrate={br} rate-control=cbr"),
                    ("vp9enc", f"vp9enc target-bitrate={br * 1000} end-usage=cbr deadline=1 cpu-used=4 threads=4")]
        if self.video_codec == "mjpeg":
            # JPEG quality is explicit; a target bitrate is not applicable.
            return [("jpegenc", "jpegenc quality=90")]
        return [("nvh264enc", f"nvh264enc bitrate={br} rc-mode=cbr ! h264parse"),
                ("vah264enc", f"vah264enc bitrate={br} rate-control=cbr ! h264parse"),
                ("x264enc", f"x264enc bitrate={br} speed-preset=veryfast tune=zerolatency key-int-max=120 ! h264parse")]
