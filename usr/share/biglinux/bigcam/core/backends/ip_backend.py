"""IP camera backend – RTSP / HTTP streams."""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlsplit

import gi

gi.require_version("Gst", "1.0")

from constants import BackendType
from gi.repository import GLib, Gst
from utils.urls import camera_url, camera_url_id, gst_quote, public_camera_name

from core.camera_backend import CameraBackend, CameraControl, CameraInfo, VideoFormat

log = logging.getLogger(__name__)


class IPBackend(CameraBackend):
    """Backend for RTSP / HTTP network cameras (manual configuration)."""

    def get_backend_type(self) -> BackendType:
        return BackendType.IP

    def is_available(self) -> bool:
        # Always available (relies on GStreamer which is already a dep)
        return True

    # -- detection -----------------------------------------------------------

    def detect_cameras(self) -> list[CameraInfo]:
        # IP cameras are configured manually; caller should provide them.
        return []

    def cameras_from_urls(self, entries: list[dict[str, str]]) -> list[CameraInfo]:
        cameras = []
        for entry in entries:
            try:
                url = camera_url(entry["url"])
                name = entry.get("name") or public_camera_name(url)
                # Legacy configurations commonly stored the credential-bearing URL as a name.
                if name == entry["url"] or "://" in name:
                    name = public_camera_name(url)
            except (KeyError, TypeError, ValueError):
                log.warning("Ignoring an invalid camera configuration")
                continue
            cameras.append(CameraInfo(id=camera_url_id(url), name=name, backend=BackendType.IP,
                                      device_path=url, capabilities=["video"], extra={"url": url}))
        return cameras

    # -- controls (none for basic IP) ----------------------------------------

    def get_controls(self, camera: CameraInfo) -> list[CameraControl]:
        return []

    def set_control(self, camera: CameraInfo, control_id: str, value: Any) -> bool:
        return False

    # -- gstreamer -----------------------------------------------------------

    def get_gst_source(self, camera: CameraInfo, fmt: VideoFormat | None = None) -> str:
        url = camera_url(camera.extra.get("url", camera.device_path))
        if urlsplit(url).scheme in {"rtsp", "rtsps"}:
            return f"rtspsrc location={gst_quote(url)} latency=150 ! decodebin ! videoconvert"
        return f"souphttpsrc location={gst_quote(url)} ! decodebin ! videoconvert"

    # -- photo ---------------------------------------------------------------

    def can_capture_photo(self) -> bool:
        return True

    @staticmethod
    def prepare_pipeline(pipeline: Gst.Pipeline) -> None:
        def element_added(_pipeline, _subbin, element):
            factory = element.get_factory()
            if factory and factory.get_name() == "multipartdemux":
                # MJPEG cameras keep a single image stream open indefinitely.
                # Let decodebin expose that pad without waiting for another MIME type.
                element.set_property("single-stream", True)
        pipeline.connect("deep-element-added", element_added)

    def capture_photo(self, camera: CameraInfo, output_path: str) -> bool:
        pipeline = None
        try:
            pipeline = Gst.parse_launch(
                f"{self.get_gst_source(camera)} ! jpegenc snapshot=true ! "
                f"filesink location={gst_quote(output_path)}")
            self.prepare_pipeline(pipeline)
            if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                return False
            message = pipeline.get_bus().timed_pop_filtered(
                15 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
            return bool(message and message.type == Gst.MessageType.EOS
                        and os.path.isfile(output_path) and os.path.getsize(output_path) > 0)
        except (OSError, ValueError, GLib.Error):
            log.warning("Network snapshot failed", exc_info=True)
            return False
        finally:
            if pipeline is not None:
                pipeline.set_state(Gst.State.NULL)
