"""Video effects pipeline — real-time OpenCV filters for camera preview."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

try:
    import cv2
    _HAS_CV2 = True
except ImportError:
    _HAS_CV2 = False


class EffectCategory(Enum):
    ADJUST = "adjust"
    FILTER = "filter"
    ARTISTIC = "artistic"
    ADVANCED = "advanced"


@dataclass
class EffectParam:
    """Describes one adjustable parameter of an effect."""
    name: str
    label: str
    min_val: float
    max_val: float
    default: float
    step: float = 1.0
    value: float = 0.0

    def __post_init__(self) -> None:
        if self.value == 0.0 and self.default != 0.0:
            self.value = self.default


@dataclass
class EffectInfo:
    """Metadata for a single effect."""
    effect_id: str
    name: str
    icon: str
    category: EffectCategory
    params: list[EffectParam] = field(default_factory=list)
    enabled: bool = False


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ── Individual effect implementations ──────────────────────────────────────

def _apply_gamma(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    gamma = _clamp(params.get("gamma", 1.0), 0.1, 5.0)
    if abs(gamma - 1.0) < 0.01:
        return frame
    inv = 1.0 / gamma
    table = np.array(
        [(i / 255.0) ** inv * 255 for i in range(256)], dtype=np.uint8,
    )
    return cv2.LUT(frame, table)


def _apply_clahe(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    clip = _clamp(params.get("clip_limit", 2.0), 1.0, 10.0)
    grid = int(_clamp(params.get("grid_size", 8), 2, 16))
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(grid, grid))
    lab[:, :, 0] = clahe.apply(lab[:, :, 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


def _apply_detail_enhance(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    sigma_s = _clamp(params.get("sigma_s", 10), 1, 200)
    sigma_r = _clamp(params.get("sigma_r", 0.15), 0.0, 1.0)
    return cv2.detailEnhance(frame, sigma_s=sigma_s, sigma_r=sigma_r)


def _apply_beauty(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    sigma_s = _clamp(params.get("sigma_s", 60), 1, 200)
    sigma_r = _clamp(params.get("sigma_r", 0.4), 0.0, 1.0)
    return cv2.edgePreservingFilter(frame, flags=1, sigma_s=sigma_s, sigma_r=sigma_r)


def _apply_brightness(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    brightness = _clamp(params.get("brightness", 0), -100, 100)
    contrast = _clamp(params.get("contrast", 0), -100, 100)
    if abs(brightness) < 1 and abs(contrast) < 1:
        return frame
    alpha = 1.0 + contrast / 100.0
    beta = brightness
    return cv2.convertScaleAbs(frame, alpha=alpha, beta=beta)


def _apply_sharpen(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    strength = _clamp(params.get("strength", 0.5), 0.0, 3.0)
    if strength < 0.01:
        return frame
    blurred = cv2.GaussianBlur(frame, (0, 0), 3)
    return cv2.addWeighted(frame, 1.0 + strength, blurred, -strength, 0)


def _apply_denoise(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    h = int(_clamp(params.get("strength", 10), 1, 30))
    return cv2.fastNlMeansDenoisingColored(frame, None, h, h, 7, 21)


def _apply_white_balance(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    wb = cv2.xphoto.createSimpleWB()
    return wb.balanceWhite(frame)


# ── Artistic effects ──

def _apply_grayscale(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def _apply_sepia(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    kernel = np.array(
        [[0.272, 0.534, 0.131],
         [0.349, 0.686, 0.168],
         [0.393, 0.769, 0.189]],
        dtype=np.float32,
    )
    return cv2.transform(frame, kernel)


def _apply_negative(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    return cv2.bitwise_not(frame)


def _apply_pencil_sketch(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    sigma_s = _clamp(params.get("sigma_s", 60), 1, 200)
    sigma_r = _clamp(params.get("sigma_r", 0.07), 0.0, 1.0)
    shade = _clamp(params.get("shade_factor", 0.05), 0.0, 0.1)
    gray, color = cv2.pencilSketch(frame, sigma_s=sigma_s, sigma_r=sigma_r,
                                   shade_factor=shade)
    return cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)


def _apply_stylization(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    sigma_s = _clamp(params.get("sigma_s", 60), 1, 200)
    sigma_r = _clamp(params.get("sigma_r", 0.45), 0.0, 1.0)
    return cv2.stylization(frame, sigma_s=sigma_s, sigma_r=sigma_r)


def _apply_cartoon(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.medianBlur(gray, 5)
    edges = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 9, 9,
    )
    color = cv2.bilateralFilter(frame, 9, 300, 300)
    return cv2.bitwise_and(color, color, mask=edges)


def _apply_edge_detect(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    t1 = int(_clamp(params.get("threshold1", 100), 0, 500))
    t2 = int(_clamp(params.get("threshold2", 200), 0, 500))
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, t1, t2)
    return cv2.cvtColor(edges, cv2.COLOR_GRAY2BGR)


def _apply_colormap(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    idx = int(_clamp(params.get("style", 0), 0, 21))
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.applyColorMap(gray, idx)


def _apply_vignette(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    strength = _clamp(params.get("strength", 0.5), 0.0, 1.0)
    if strength < 0.01:
        return frame
    h, w = frame.shape[:2]
    x = np.arange(w, dtype=np.float32) - w / 2
    y = np.arange(h, dtype=np.float32) - h / 2
    xx, yy = np.meshgrid(x, y)
    radius = np.sqrt(xx**2 + yy**2)
    max_r = np.sqrt((w / 2) ** 2 + (h / 2) ** 2)
    mask = 1.0 - strength * (radius / max_r) ** 2
    mask = np.clip(mask, 0, 1)
    return (frame * mask[:, :, np.newaxis]).astype(np.uint8)


# ── Advanced / Background effects ──

_face_detector: Any = None


def _get_face_detector() -> Any:
    global _face_detector
    if _face_detector is None:
        model_path = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        _face_detector = cv2.CascadeClassifier(model_path)
    return _face_detector


def _apply_bg_blur(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    strength = int(_clamp(params.get("strength", 21), 1, 51))
    if strength % 2 == 0:
        strength += 1

    detector = _get_face_detector()
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    eq = cv2.equalizeHist(gray)
    faces = detector.detectMultiScale(
        eq, scaleFactor=1.1, minNeighbors=4, minSize=(50, 50),
    )

    if len(faces) == 0:
        # No face detected — blur entire frame lightly as feedback
        return cv2.GaussianBlur(frame, (strength, strength), 0)

    blurred = cv2.GaussianBlur(frame, (strength, strength), 0)
    mask = np.zeros(frame.shape[:2], dtype=np.uint8)

    for (fx, fy, fw, fh) in faces:
        margin_x = int(fw * 0.5)
        margin_y = int(fh * 0.8)
        cx, cy = fx + fw // 2, fy + fh // 2
        axes = (fw // 2 + margin_x, fh // 2 + margin_y)
        cv2.ellipse(mask, (cx, cy), axes, 0, 0, 360, 255, -1)

    mask_blur = cv2.GaussianBlur(mask, (31, 31), 0)
    mask_f = mask_blur.astype(np.float32) / 255.0
    mask_3 = mask_f[:, :, np.newaxis]
    result = (frame.astype(np.float32) * mask_3 +
              blurred.astype(np.float32) * (1.0 - mask_3))
    return result.astype(np.uint8)


# ── Effect registry ────────────────────────────────────────────────────────

_EFFECTS_REGISTRY: list[tuple[EffectInfo, Any]] = []


def _register_effects() -> None:
    global _EFFECTS_REGISTRY

    _EFFECTS_REGISTRY = [
        # ── Adjust ──
        (EffectInfo(
            effect_id="brightness",
            name="Brightness / Contrast",
            icon="display-brightness-symbolic",
            category=EffectCategory.ADJUST,
            params=[
                EffectParam("brightness", "Brightness", -100, 100, 0, 1),
                EffectParam("contrast", "Contrast", -100, 100, 0, 1),
            ],
        ), _apply_brightness),

        (EffectInfo(
            effect_id="gamma",
            name="Gamma Correction",
            icon="preferences-color-symbolic",
            category=EffectCategory.ADJUST,
            params=[
                EffectParam("gamma", "Gamma", 0.1, 5.0, 1.0, 0.1),
            ],
        ), _apply_gamma),

        (EffectInfo(
            effect_id="clahe",
            name="CLAHE (Adaptive Contrast)",
            icon="image-adjust-contrast",
            category=EffectCategory.ADJUST,
            params=[
                EffectParam("clip_limit", "Clip Limit", 1.0, 10.0, 2.0, 0.5),
                EffectParam("grid_size", "Grid Size", 2, 16, 8, 1),
            ],
        ), _apply_clahe),

        (EffectInfo(
            effect_id="white_balance",
            name="Auto White Balance",
            icon="weather-clear-symbolic",
            category=EffectCategory.ADJUST,
        ), _apply_white_balance),

        # ── Filter ──
        (EffectInfo(
            effect_id="detail_enhance",
            name="Detail Enhance",
            icon="find-location-symbolic",
            category=EffectCategory.FILTER,
            params=[
                EffectParam("sigma_s", "Smoothing", 1, 200, 10, 5),
                EffectParam("sigma_r", "Detail", 0.0, 1.0, 0.15, 0.05),
            ],
        ), _apply_detail_enhance),

        (EffectInfo(
            effect_id="beauty",
            name="Beauty / Soft Skin",
            icon="face-smile-symbolic",
            category=EffectCategory.FILTER,
            params=[
                EffectParam("sigma_s", "Smoothing", 1, 200, 60, 10),
                EffectParam("sigma_r", "Intensity", 0.0, 1.0, 0.4, 0.05),
            ],
        ), _apply_beauty),

        (EffectInfo(
            effect_id="sharpen",
            name="Sharpen",
            icon="image-sharpen-symbolic",
            category=EffectCategory.FILTER,
            params=[
                EffectParam("strength", "Strength", 0.0, 3.0, 0.5, 0.1),
            ],
        ), _apply_sharpen),

        (EffectInfo(
            effect_id="denoise",
            name="Denoise",
            icon="audio-volume-muted-symbolic",
            category=EffectCategory.FILTER,
            params=[
                EffectParam("strength", "Strength", 1, 30, 10, 1),
            ],
        ), _apply_denoise),

        # ── Artistic ──
        (EffectInfo(
            effect_id="grayscale",
            name="Grayscale",
            icon="bwtonal",
            category=EffectCategory.ARTISTIC,
        ), _apply_grayscale),

        (EffectInfo(
            effect_id="sepia",
            name="Sepia",
            icon="accessories-text-editor-symbolic",
            category=EffectCategory.ARTISTIC,
        ), _apply_sepia),

        (EffectInfo(
            effect_id="negative",
            name="Negative",
            icon="view-refresh-symbolic",
            category=EffectCategory.ARTISTIC,
        ), _apply_negative),

        (EffectInfo(
            effect_id="pencil_sketch",
            name="Pencil Sketch",
            icon="edit-select-symbolic",
            category=EffectCategory.ARTISTIC,
            params=[
                EffectParam("sigma_s", "Smoothing", 1, 200, 60, 10),
                EffectParam("sigma_r", "Detail", 0.0, 1.0, 0.07, 0.01),
                EffectParam("shade_factor", "Shade", 0.0, 0.1, 0.05, 0.01),
            ],
        ), _apply_pencil_sketch),

        (EffectInfo(
            effect_id="stylization",
            name="Painting",
            icon="applications-graphics-symbolic",
            category=EffectCategory.ARTISTIC,
            params=[
                EffectParam("sigma_s", "Smoothing", 1, 200, 60, 10),
                EffectParam("sigma_r", "Detail", 0.0, 1.0, 0.45, 0.05),
            ],
        ), _apply_stylization),

        (EffectInfo(
            effect_id="cartoon",
            name="Cartoon",
            icon="face-laugh-symbolic",
            category=EffectCategory.ARTISTIC,
        ), _apply_cartoon),

        (EffectInfo(
            effect_id="edge_detect",
            name="Edge Detection",
            icon="emblem-photos-symbolic",
            category=EffectCategory.ARTISTIC,
            params=[
                EffectParam("threshold1", "Threshold 1", 0, 500, 100, 10),
                EffectParam("threshold2", "Threshold 2", 0, 500, 200, 10),
            ],
        ), _apply_edge_detect),

        (EffectInfo(
            effect_id="colormap",
            name="Color Map",
            icon="preferences-color-symbolic",
            category=EffectCategory.ARTISTIC,
            params=[
                EffectParam("style", "Style", 0, 21, 0, 1),
            ],
        ), _apply_colormap),

        (EffectInfo(
            effect_id="vignette",
            name="Vignette",
            icon="camera-photo-symbolic",
            category=EffectCategory.ARTISTIC,
            params=[
                EffectParam("strength", "Strength", 0.0, 1.0, 0.5, 0.05),
            ],
        ), _apply_vignette),

        # ── Advanced ──
        (EffectInfo(
            effect_id="bg_blur",
            name="Background Blur",
            icon="camera-web-symbolic",
            category=EffectCategory.ADVANCED,
            params=[
                EffectParam("strength", "Blur Strength", 1, 51, 21, 2),
            ],
        ), _apply_bg_blur),
    ]


class EffectPipeline:
    """Manages a chain of OpenCV effects applied to each video frame."""

    def __init__(self) -> None:
        self._effects: list[tuple[EffectInfo, Any]] = []
        if _HAS_CV2:
            _register_effects()
            self._effects = list(_EFFECTS_REGISTRY)

    @property
    def available(self) -> bool:
        return _HAS_CV2

    def get_effects(self) -> list[EffectInfo]:
        return [info for info, _ in self._effects]

    def get_effect(self, effect_id: str) -> EffectInfo | None:
        for info, _ in self._effects:
            if info.effect_id == effect_id:
                return info
        return None

    def set_enabled(self, effect_id: str, enabled: bool) -> None:
        for info, _ in self._effects:
            if info.effect_id == effect_id:
                info.enabled = enabled
                return

    def set_param(self, effect_id: str, param_name: str, value: float) -> None:
        for info, _ in self._effects:
            if info.effect_id == effect_id:
                for p in info.params:
                    if p.name == param_name:
                        p.value = _clamp(value, p.min_val, p.max_val)
                        return

    def reset_effect(self, effect_id: str) -> None:
        for info, _ in self._effects:
            if info.effect_id == effect_id:
                for p in info.params:
                    p.value = p.default
                return

    def reset_all(self) -> None:
        for info, _ in self._effects:
            info.enabled = False
            for p in info.params:
                p.value = p.default

    def has_active_effects(self) -> bool:
        return any(info.enabled for info, _ in self._effects)

    def apply(self, frame: np.ndarray) -> np.ndarray:
        """Apply all enabled effects to a BGR frame."""
        if not _HAS_CV2:
            return frame
        for info, func in self._effects:
            if not info.enabled:
                continue
            params = {p.name: p.value for p in info.params}
            try:
                frame = func(frame, params)
            except Exception:
                pass
        return frame

    def apply_bgra(self, data: bytes, width: int, height: int) -> bytes:
        """Apply effects to raw BGRA pixel data, return processed BGRA bytes."""
        if not _HAS_CV2 or not self.has_active_effects():
            return data
        try:
            arr = np.frombuffer(data, dtype=np.uint8).reshape((height, width, 4))
            bgr = arr[:, :, :3].copy()
            alpha = arr[:, :, 3].copy()
            bgr = self.apply(bgr)
            result = np.dstack((bgr, alpha))
            return result.tobytes()
        except Exception:
            return data
