"""Video effects pipeline — real-time OpenCV filters for camera preview."""

from __future__ import annotations

import logging
import os
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

from utils.i18n import _

log = logging.getLogger(__name__)

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

# Per-parameter caches — avoid per-frame allocations
_gamma_lut_cache: dict[float, np.ndarray] = {}
_clahe_cache: dict[tuple, Any] = {}
_vignette_cache: dict[tuple, np.ndarray] = {}
_wb_processor = cv2.xphoto.createSimpleWB() if _HAS_CV2 else None
_SEPIA_KERNEL = (
    np.array(
        [[0.272, 0.534, 0.131], [0.349, 0.686, 0.168], [0.393, 0.769, 0.189]],
        dtype=np.float32,
    )
    if _HAS_CV2
    else None
)


def _apply_gamma(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    gamma = _clamp(params.get("gamma", 1.0), 0.1, 5.0)
    if abs(gamma - 1.0) < 0.01:
        return frame
    inv = 1.0 / gamma
    key = round(inv, 4)
    if key not in _gamma_lut_cache:
        if len(_gamma_lut_cache) >= 8:
            _gamma_lut_cache.pop(next(iter(_gamma_lut_cache)))
        _gamma_lut_cache[key] = np.array(
            [(i / 255.0) ** inv * 255 for i in range(256)],
            dtype=np.uint8,
        )
    return cv2.LUT(frame, _gamma_lut_cache[key])


def _apply_clahe(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    clip = _clamp(params.get("clip_limit", 2.0), 1.0, 10.0)
    grid = int(_clamp(params.get("grid_size", 8), 2, 16))
    key = (round(clip, 2), grid)
    if key not in _clahe_cache:
        if len(_clahe_cache) >= 8:
            _clahe_cache.pop(next(iter(_clahe_cache)))
        _clahe_cache[key] = cv2.createCLAHE(clipLimit=clip, tileGridSize=(grid, grid))
    clahe = _clahe_cache[key]
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    lab[:, :, 0] = clahe.apply(lab[:, :, 0])
    return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)


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
    h_val = int(_clamp(params.get("strength", 10), 1, 30))
    d = max(5, h_val // 2)
    sigma = h_val * 7.5
    h, w = frame.shape[:2]
    scale = 0.5 if min(h, w) > 480 else 1.0
    if scale < 1.0:
        small = cv2.resize(frame, (w // 2, h // 2), interpolation=cv2.INTER_LINEAR)
        smooth = cv2.bilateralFilter(small, d, sigma, sigma)
        return cv2.resize(smooth, (w, h), interpolation=cv2.INTER_LINEAR)
    return cv2.bilateralFilter(frame, d, sigma, sigma)


def _apply_white_balance(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    return _wb_processor.balanceWhite(frame)


# ── Artistic effects ──


def _apply_grayscale(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.merge([gray, gray, gray])


def _apply_sepia(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    return cv2.transform(frame, _SEPIA_KERNEL)


def _apply_negative(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    return cv2.bitwise_not(frame)


def _apply_cartoon(frame: np.ndarray, params: dict[str, float]) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.medianBlur(gray, 5)
    edges = cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY,
        9,
        9,
    )
    color = cv2.bilateralFilter(frame, 7, 300, 300)
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
    raw = _clamp(params.get("strength", 50), 10, 100)
    strength = (raw / 100.0) * 3.0
    h, w = frame.shape[:2]
    key = (w, h, round(strength, 2))
    if key not in _vignette_cache:
        # Cap cache size to prevent unbounded memory growth (~8MB per entry at 1080p)
        if len(_vignette_cache) >= 4:
            _vignette_cache.pop(next(iter(_vignette_cache)))
        x = np.arange(w, dtype=np.float32) - w / 2
        y = np.arange(h, dtype=np.float32) - h / 2
        xx, yy = np.meshgrid(x, y)
        radius = np.sqrt(xx**2 + yy**2)
        max_r = np.sqrt((w / 2) ** 2 + (h / 2) ** 2)
        mask = 1.0 - strength * (radius / max_r) ** 2
        _vignette_cache[key] = np.clip(mask, 0, 1)
    mask = _vignette_cache[key]
    mask3 = cv2.merge([mask, mask, mask])
    return cv2.multiply(frame, mask3, dtype=cv2.CV_8U)



def release_segmenter() -> None:
    """Release effect caches to free memory."""
    _vignette_cache.clear()
    _gamma_lut_cache.clear()
    _clahe_cache.clear()


# ── Effect registry ────────────────────────────────────────────────────────

_EFFECTS_REGISTRY: list[tuple[EffectInfo, Any]] = []


def _register_effects() -> None:
    global _EFFECTS_REGISTRY

    _EFFECTS_REGISTRY = [
        # ── Adjust ──
        (
            EffectInfo(
                effect_id="brightness",
                name=_("Brightness / Contrast"),
                icon="display-brightness-symbolic",
                category=EffectCategory.ADJUST,
                params=[
                    EffectParam("brightness", _("Brightness"), -100, 100, 0, 1),
                    EffectParam("contrast", _("Contrast"), -100, 100, 0, 1),
                ],
            ),
            _apply_brightness,
        ),
        (
            EffectInfo(
                effect_id="gamma",
                name=_("Gamma Correction"),
                icon="preferences-color-symbolic",
                category=EffectCategory.ADJUST,
                params=[
                    EffectParam("gamma", _("Gamma"), 0.1, 5.0, 1.0, 0.1),
                ],
            ),
            _apply_gamma,
        ),
        (
            EffectInfo(
                effect_id="clahe",
                name=_("CLAHE (Adaptive Contrast)"),
                icon="image-adjust-contrast",
                category=EffectCategory.ADJUST,
                params=[
                    EffectParam("clip_limit", _("Clip Limit"), 0.5, 4.0, 1.0, 0.1),
                    EffectParam("grid_size", _("Grid Size"), 2, 16, 8, 1),
                ],
            ),
            _apply_clahe,
        ),
        (
            EffectInfo(
                effect_id="white_balance",
                name=_("Auto White Balance"),
                icon="weather-clear-symbolic",
                category=EffectCategory.ADJUST,
            ),
            _apply_white_balance,
        ),
        # ── Filter ──
        (
            EffectInfo(
                effect_id="sharpen",
                name=_("Sharpen"),
                icon="image-sharpen-symbolic",
                category=EffectCategory.FILTER,
                params=[
                    EffectParam("strength", _("Strength"), 0.0, 3.0, 0.5, 0.1),
                ],
            ),
            _apply_sharpen,
        ),
        (
            EffectInfo(
                effect_id="denoise",
                name=_("Denoise"),
                icon="audio-volume-muted-symbolic",
                category=EffectCategory.FILTER,
                params=[
                    EffectParam("strength", _("Strength"), 1, 30, 10, 1),
                ],
            ),
            _apply_denoise,
        ),
        # ── Artistic ──
        (
            EffectInfo(
                effect_id="grayscale",
                name=_("Grayscale"),
                icon="bwtonal",
                category=EffectCategory.ARTISTIC,
            ),
            _apply_grayscale,
        ),
        (
            EffectInfo(
                effect_id="sepia",
                name=_("Sepia"),
                icon="accessories-text-editor-symbolic",
                category=EffectCategory.ARTISTIC,
            ),
            _apply_sepia,
        ),
        (
            EffectInfo(
                effect_id="negative",
                name=_("Negative"),
                icon="view-refresh-symbolic",
                category=EffectCategory.ARTISTIC,
            ),
            _apply_negative,
        ),
        (
            EffectInfo(
                effect_id="edge_detect",
                name=_("Edge Detection"),
                icon="emblem-photos-symbolic",
                category=EffectCategory.ARTISTIC,
                params=[
                    EffectParam("threshold1", _("Threshold 1"), 0, 500, 100, 10),
                    EffectParam("threshold2", _("Threshold 2"), 0, 500, 200, 10),
                ],
            ),
            _apply_edge_detect,
        ),
        (
            EffectInfo(
                effect_id="colormap",
                name=_("Color Map"),
                icon="preferences-color-symbolic",
                category=EffectCategory.ARTISTIC,
                params=[
                    EffectParam("style", _("Style"), 0, 21, 0, 1),
                ],
            ),
            _apply_colormap,
        ),
        (
            EffectInfo(
                effect_id="vignette",
                name=_("Vignette"),
                icon="camera-photo-symbolic",
                category=EffectCategory.ARTISTIC,
                params=[
                    EffectParam("strength", _("Strength"), 10, 100, 50, 5),
                ],
            ),
            _apply_vignette,
        ),
        # ── Advanced ──

    ]


class EffectPipeline:
    """Manages a chain of OpenCV effects applied to each video frame."""

    def __init__(self) -> None:
        self._effects: list[tuple[EffectInfo, Any]] = []
        self._active_count: int = 0
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
                if info.enabled != enabled:
                    self._active_count += 1 if enabled else -1
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
        self._active_count = 0
        # Free cached data to reduce memory
        release_segmenter()
        _clahe_cache.clear()

    def has_active_effects(self) -> bool:
        return self._active_count > 0

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
                log.debug("Effect %s failed", info.name, exc_info=True)
        return frame

    def apply_bgra(self, data: bytes, width: int, height: int) -> bytes:
        """Apply effects to raw BGRA pixel data, return processed BGRA bytes."""
        if not _HAS_CV2 or not self.has_active_effects():
            return data
        try:
            arr = np.frombuffer(data, dtype=np.uint8).reshape((height, width, 4))
            bgr = self.apply(arr[:, :, :3].copy())
            result = cv2.merge([bgr[:, :, 0], bgr[:, :, 1], bgr[:, :, 2], arr[:, :, 3]])
            return result.tobytes()
        except Exception:
            return data
