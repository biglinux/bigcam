"""PipeWire backend – virtual cameras, OBS, XDP camera portal."""

from __future__ import annotations

import re
import subprocess
from typing import Any

from constants import BackendType
from core.camera_backend import CameraBackend, CameraControl, CameraInfo, VideoFormat


class PipeWireBackend(CameraBackend):
    """Backend for PipeWire video source nodes."""

    def get_backend_type(self) -> BackendType:
        return BackendType.PIPEWIRE

    def is_available(self) -> bool:
        try:
            subprocess.run(["pw-cli", "info", "0"], capture_output=True, timeout=5)
            return True
        except (
            FileNotFoundError,
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
        ):
            return False

    # -- detection -----------------------------------------------------------

    def detect_cameras(self) -> list[CameraInfo]:
        cameras: list[CameraInfo] = []
        try:
            result = subprocess.run(
                ["pw-cli", "list-objects"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return cameras

            # Parse pw-cli output for Video/Source nodes
            cameras = self._parse_pw_objects(result.stdout)
        except Exception:
            pass
        return cameras

    def _parse_pw_objects(self, output: str) -> list[CameraInfo]:
        cameras: list[CameraInfo] = []
        current_id = ""
        current_props: dict[str, str] = {}

        for line in output.splitlines():
            # New object: "id 42, type PipeWire:Interface:Node/3"
            obj_match = re.match(
                r"\s*id\s+(\d+),\s+type\s+PipeWire:Interface:Node", line
            )
            if obj_match:
                # Flush previous
                if current_id and self._is_video_source(current_props):
                    cam = self._make_camera(current_id, current_props)
                    if (
                        "v4l2loopback" not in cam.name.lower()
                        and "(v4l2)" not in cam.name.lower()
                    ):
                        cameras.append(cam)
                current_id = obj_match.group(1)
                current_props = {}
                continue

            # Property: "    media.class = \"Video/Source\""
            prop_match = re.match(r'\s+([\w.]+)\s*=\s*"?([^"]*)"?', line)
            if prop_match and current_id:
                current_props[prop_match.group(1)] = prop_match.group(2).strip()

        # Flush last
        if current_id and self._is_video_source(current_props):
            cam = self._make_camera(current_id, current_props)
            if (
                "v4l2loopback" not in cam.name.lower()
                and "(v4l2)" not in cam.name.lower()
            ):
                cameras.append(cam)

        return cameras

    @staticmethod
    def _is_video_source(props: dict[str, str]) -> bool:
        return props.get("media.class", "") in ("Video/Source", "Video/Source/Virtual")

    @staticmethod
    def _make_camera(node_id: str, props: dict[str, str]) -> CameraInfo:
        name = props.get(
            "node.description",
            props.get("node.nick", props.get("node.name", f"PipeWire Node {node_id}")),
        )
        return CameraInfo(
            id=f"pipewire:{node_id}",
            name=name,
            backend=BackendType.PIPEWIRE,
            device_path=node_id,
            capabilities=["video"],
            is_virtual="Virtual" in props.get("media.class", ""),
            extra={"node_id": node_id, **props},
        )

    # -- controls (PipeWire doesn't expose camera controls) ------------------

    def get_controls(self, camera: CameraInfo) -> list[CameraControl]:
        return []

    def set_control(self, camera: CameraInfo, control_id: str, value: Any) -> bool:
        return False

    # -- gstreamer -----------------------------------------------------------

    def get_gst_source(self, camera: CameraInfo, fmt: VideoFormat | None = None) -> str:
        node_id = camera.extra.get("node_id", camera.device_path)
        return f"pipewiresrc path={node_id}"

    # -- photo ---------------------------------------------------------------

    def can_capture_photo(self) -> bool:
        return True

    def capture_photo(self, camera: CameraInfo, output_path: str) -> bool:
        """Snapshot via GStreamer pipeline."""
        node_id = camera.extra.get("node_id", camera.device_path)
        try:
            subprocess.run(
                [
                    "gst-launch-1.0",
                    "-e",
                    "pipewiresrc",
                    f"path={node_id}",
                    "num-buffers=1",
                    "!",
                    "videoconvert",
                    "!",
                    "jpegenc",
                    "!",
                    "filesink",
                    f"location={output_path}",
                ],
                capture_output=True,
                check=True,
                timeout=10,
            )
            import os

            return os.path.isfile(output_path)
        except Exception:
            return False
