"""Photo capture orchestrator – multi-backend photo acquisition."""

from __future__ import annotations

import os
import time

from core.camera_backend import CameraInfo
from core.camera_manager import CameraManager
from utils import xdg


class PhotoCapture:
    """Handles photo capture across all backends with filename generation."""

    def __init__(self, camera_manager: CameraManager) -> None:
        self._manager = camera_manager

    def capture(self, camera: CameraInfo, filename: str | None = None) -> str | None:
        """Capture a photo. Returns the output path on success, None on failure."""
        if not self._manager.can_capture_photo(camera):
            return None

        if filename is None:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            ext = "jpg"
            filename = f"bigcam_{timestamp}.{ext}"

        output_dir = xdg.photos_dir()
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, filename)

        ok = self._manager.capture_photo(camera, output_path)
        return output_path if ok else None
