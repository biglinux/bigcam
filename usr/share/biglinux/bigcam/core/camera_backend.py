"""Abstract base class and data models for camera backends."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from constants import BackendType, ControlCategory, ControlType


@dataclass
class CameraControl:
    id: str
    name: str
    category: ControlCategory
    control_type: ControlType
    value: Any
    default: Any
    minimum: int | None = None
    maximum: int | None = None
    step: int = 1
    choices: list[str] | None = None
    flags: str = ""


@dataclass
class VideoFormat:
    width: int
    height: int
    fps: list[float]
    pixel_format: str
    description: str = ""


@dataclass
class CameraInfo:
    id: str
    name: str
    backend: BackendType
    device_path: str
    capabilities: list[str] = field(default_factory=list)
    formats: list[VideoFormat] = field(default_factory=list)
    is_virtual: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


class CameraBackend(ABC):
    """Interface that every camera backend must implement."""

    @abstractmethod
    def get_backend_type(self) -> BackendType: ...

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def detect_cameras(self) -> list[CameraInfo]: ...

    @abstractmethod
    def get_controls(self, camera: CameraInfo) -> list[CameraControl]: ...

    @abstractmethod
    def set_control(self, camera: CameraInfo, control_id: str, value: Any) -> bool: ...

    @abstractmethod
    def get_gst_source(self, camera: CameraInfo, fmt: VideoFormat | None = None) -> str: ...

    @abstractmethod
    def can_capture_photo(self) -> bool: ...

    @abstractmethod
    def capture_photo(self, camera: CameraInfo, output_path: str) -> bool: ...

    def reset_control(self, camera: CameraInfo, control_id: str, controls: list[CameraControl]) -> bool:
        """Reset a single control to its default value."""
        for ctrl in controls:
            if ctrl.id == control_id:
                return self.set_control(camera, control_id, ctrl.default)
        return False

    def reset_all_controls(self, camera: CameraInfo, controls: list[CameraControl]) -> None:
        """Reset every control to its default value."""
        for ctrl in controls:
            if ctrl.flags not in ("inactive", "read-only"):
                self.set_control(camera, ctrl.id, ctrl.default)
