"""Automatic image enhancement for the camera preview.

Webcams meter badly: backlit scenes come out dark, cheap sensors drift toward
a blue or green cast, and low-contrast rooms look hazy.  This module measures
each frame and applies three cheap, well-understood corrections, but only as
far as the frame actually needs:

* **auto gamma** — moves median luminance toward a mid target.  Gamma is used
  rather than a linear gain because it lifts shadows without clipping
  highlights.
* **CLAHE** on the L channel of LAB — restores local contrast in flat frames.
  Applied to L only, so hue is untouched.
* **gray-world white balance** — assumes the average scene is neutral grey and
  scales the B and R channels to match G, removing a colour cast.

Two properties matter as much as the corrections themselves:

*Do no harm.*  A well-exposed, neutral frame must come out visually identical;
every stage has a dead-band and is skipped when the measurement sits inside it.

*Do not flicker.*  Correcting each frame independently makes the picture pulse
as the scene changes.  All parameters are smoothed with an exponential moving
average, so the correction eases in over roughly a second instead of snapping.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)

try:
    import cv2

    _HAS_CV2 = True
except ImportError:  # pragma: no cover - exercised only on broken installs
    _HAS_CV2 = False

# Target median luminance.  Slightly below the midpoint: faces read better a
# touch darker than 128, and it leaves headroom before highlights clip.
_TARGET_LUMA = 120.0

# Dead-bands.  Inside these the frame is considered fine and left alone.
_LUMA_TOLERANCE = 12.0      # ±12 levels around the target
_CAST_TOLERANCE = 0.06      # 6% channel imbalance
_CONTRAST_FLOOR = 42.0      # luma std-dev below this counts as flat
_SATURATION_FLOOR = 28.0    # mean HSV S below this looks washed out

# Clamps, so a pathological frame cannot produce a wild correction.
_MIN_GAMMA, _MAX_GAMMA = 0.45, 2.2
_MAX_CHANNEL_GAIN = 1.6
# Cap on chroma boost.  Beyond this, colour noise in a dim frame becomes more
# objectionable than the missing saturation it is meant to fix.
_MAX_SATURATION_BOOST = 1.8

# Exponential-moving-average factor for parameter smoothing.  At 30 fps this
# reaches ~95% of a new target in about one second.
_SMOOTHING = 0.10

# Analysis runs on a downscaled copy: the statistics are unchanged for our
# purposes and it keeps the cost flat regardless of capture resolution.
_ANALYSIS_MAX_DIM = 256


@dataclass(frozen=True)
class FrameStats:
    """What a single frame measurement tells us."""

    median_luma: float
    contrast: float
    colour_cast: float
    channel_means: tuple[float, float, float]
    saturation: float = 0.0

    @property
    def needs_exposure(self) -> bool:
        return abs(self.median_luma - _TARGET_LUMA) > _LUMA_TOLERANCE

    @property
    def needs_contrast(self) -> bool:
        return self.contrast < _CONTRAST_FLOOR

    @property
    def needs_white_balance(self) -> bool:
        return self.colour_cast > _CAST_TOLERANCE

    @property
    def needs_saturation(self) -> bool:
        return self.saturation < _SATURATION_FLOOR


def _downscale(bgr: np.ndarray) -> np.ndarray:
    """Shrink *bgr* so the longest side is at most _ANALYSIS_MAX_DIM.

    Uses strided slicing rather than cv2.resize: we only need medians and
    means, which nearest-neighbour sampling estimates just as well, and
    slicing a 1080p frame is roughly ten times cheaper than an INTER_AREA
    resize.
    """
    h, w = bgr.shape[:2]
    step = max(1, (max(h, w) + _ANALYSIS_MAX_DIM - 1) // _ANALYSIS_MAX_DIM)
    if step == 1:
        return bgr
    return np.ascontiguousarray(bgr[::step, ::step])


def analyse_frame(bgr: np.ndarray) -> FrameStats:
    """Measure exposure, contrast and colour balance of a BGR frame.

    Cheap by construction: the frame is downscaled first, so a 1080p input
    costs the same as a 256-pixel one.
    """
    small = _downscale(bgr)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

    median_luma = float(np.median(gray))
    contrast = float(gray.std())
    # Mean HSV S.  Dim scenes and high sensor gain both drain chroma, and the
    # difference is invisible in a luminance-only measurement.
    saturation = float(cv2.cvtColor(small, cv2.COLOR_BGR2HSV)[:, :, 1].mean())

    means = small.reshape(-1, 3).mean(axis=0)
    b, g, r = (float(x) for x in means)
    average = (b + g + r) / 3.0
    # Relative spread between channels: 0 for a neutral frame, growing with
    # the strength of any cast.  Guarded against a fully black frame.
    cast = (max(b, g, r) - min(b, g, r)) / average if average > 1.0 else 0.0

    return FrameStats(
        median_luma=median_luma,
        contrast=contrast,
        colour_cast=cast,
        channel_means=(b, g, r),
        saturation=saturation,
    )


class AutoEnhancer:
    """Stateful auto-exposure / auto-contrast / auto-white-balance.

    Stateful on purpose: the smoothing that prevents flicker needs to remember
    the previous correction.  Create one per stream and call :meth:`reset`
    when the camera changes.
    """

    def __init__(self) -> None:
        self._gamma = 1.0
        self._gain_b = 1.0
        self._gain_r = 1.0
        self._clahe_strength = 0.0
        self._saturation_boost = 1.0
        self._lut: np.ndarray | None = None
        self._lut_key: tuple | None = None
        self._clahe = None
        self._frame_index = 0
        self._stats: FrameStats | None = None

    # -- introspection -----------------------------------------------------

    @property
    def gain(self) -> float:
        """Current smoothed gamma — used by tests and the UI readout."""
        return self._gamma

    @property
    def last_stats(self) -> FrameStats | None:
        return self._stats

    def reset(self) -> None:
        """Forget all adaptation; the next frame starts from neutral."""
        self._gamma = 1.0
        self._gain_b = 1.0
        self._gain_r = 1.0
        self._clahe_strength = 0.0
        self._saturation_boost = 1.0
        self._frame_index = 0
        self._stats = None

    # -- main entry point --------------------------------------------------

    def process(self, bgr: np.ndarray) -> np.ndarray:
        """Return an enhanced copy of *bgr*.

        The input is never modified.  Anything that is not an 8-bit 3-channel
        image is passed straight through, so an unexpected format degrades to
        a no-op rather than an exception in the preview path.
        """
        if not _HAS_CV2:
            return bgr
        if bgr is None or bgr.ndim != 3 or bgr.shape[2] != 3:
            return bgr
        if bgr.dtype != np.uint8:
            return bgr

        try:
            self._update_targets(analyse_frame(bgr))
            return self._apply(bgr)
        except cv2.error:
            log.debug("Auto-enhance failed on this frame", exc_info=True)
            return bgr

    # -- measurement -> smoothed parameters --------------------------------

    def _update_targets(self, stats: FrameStats) -> None:
        self._stats = stats
        self._frame_index += 1

        # Exposure: the gamma that would move the measured median onto the
        # target, derived from  (median/255) ** gamma == target/255.
        # A pure-white frame gives log(1.0) == 0 in the denominator; clamp just
        # below 255 so the division stays finite.
        luma = float(np.clip(stats.median_luma, 1.0, 254.0))
        if stats.needs_exposure:
            ratio = np.log(_TARGET_LUMA / 255.0) / np.log(luma / 255.0)
            target_gamma = float(np.clip(ratio, _MIN_GAMMA, _MAX_GAMMA))
        else:
            target_gamma = 1.0

        # White balance: scale B and R so their means meet G's (gray-world).
        if stats.needs_white_balance:
            b, g, r = stats.channel_means
            target_b = float(np.clip(g / b, 1 / _MAX_CHANNEL_GAIN,
                                     _MAX_CHANNEL_GAIN)) if b > 1.0 else 1.0
            target_r = float(np.clip(g / r, 1 / _MAX_CHANNEL_GAIN,
                                     _MAX_CHANNEL_GAIN)) if r > 1.0 else 1.0
        else:
            target_b = target_r = 1.0

        # Chroma: dim rooms and sensor gain both wash colour out.  Scale back
        # up toward the floor, capped so colour noise is not amplified.
        if stats.needs_saturation and stats.saturation > 1.0:
            target_sat = float(np.clip(
                _SATURATION_FLOOR / stats.saturation, 1.0, _MAX_SATURATION_BOOST
            ))
        else:
            target_sat = 1.0

        # Contrast: how hard CLAHE should push, 0..1 by how flat the frame is.
        if stats.needs_contrast:
            deficit = (_CONTRAST_FLOOR - stats.contrast) / _CONTRAST_FLOOR
            target_clahe = float(np.clip(deficit, 0.0, 1.0))
        else:
            target_clahe = 0.0

        # First frame jumps straight to the measurement so the preview does
        # not open visibly wrong and then drift; afterwards, ease in.
        alpha = 1.0 if self._frame_index == 1 else _SMOOTHING
        self._gamma += (target_gamma - self._gamma) * alpha
        self._gain_b += (target_b - self._gain_b) * alpha
        self._gain_r += (target_r - self._gain_r) * alpha
        self._clahe_strength += (target_clahe - self._clahe_strength) * alpha
        self._saturation_boost += (target_sat - self._saturation_boost) * alpha

    # -- parameters -> pixels ----------------------------------------------

    def _tone_lut(self, gamma: float, gain_b: float, gain_r: float) -> np.ndarray:
        """A 256x1x3 LUT combining white balance and gamma.

        Both are per-pixel, per-channel point operations, so they compose into
        a single table and cost one ``cv2.LUT`` pass over the frame.  Doing the
        white balance as float32 arithmetic instead measured ~11.6 ms on a
        1080p frame against ~0.5 ms for the lookup.

        The table is rebuilt only when a parameter moves perceptibly, so the
        smoothing ramp does not rebuild it 30 times a second.
        """
        key = (round(gamma, 3), round(gain_b, 3), round(gain_r, 3))
        if self._lut is None or self._lut_key != key:
            index = np.arange(256, dtype=np.float32) / 255.0
            curve = np.power(index, gamma) * 255.0
            table = np.empty((256, 1, 3), dtype=np.uint8)
            # OpenCV channel order is B, G, R.
            for channel, gain in ((0, gain_b), (1, 1.0), (2, gain_r)):
                table[:, 0, channel] = np.clip(curve * gain, 0, 255).astype(np.uint8)
            self._lut = table
            self._lut_key = key
        return self._lut

    def _apply(self, bgr: np.ndarray) -> np.ndarray:
        out = bgr

        # 1. Exposure and white balance, folded into one lookup.
        needs_tone = (
            abs(self._gamma - 1.0) > 0.01
            or abs(self._gain_b - 1.0) > 0.01
            or abs(self._gain_r - 1.0) > 0.01
        )
        if needs_tone:
            out = cv2.LUT(out, self._tone_lut(self._gamma, self._gain_b, self._gain_r))

        # 2. Local contrast, on L only so colours are preserved.
        if self._clahe_strength > 0.02:
            clip = 1.0 + 2.0 * self._clahe_strength
            if self._clahe is None:
                self._clahe = cv2.createCLAHE(clipLimit=clip, tileGridSize=(8, 8))
            else:
                self._clahe.setClipLimit(clip)
            lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
            lab[:, :, 0] = self._clahe.apply(lab[:, :, 0])
            out = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

        # 3. Chroma. Scaling S in HSV leaves luminance and hue untouched, so
        #    this cannot undo the exposure work above.
        if self._saturation_boost > 1.02:
            hsv = cv2.cvtColor(out, cv2.COLOR_BGR2HSV)
            hsv[:, :, 1] = cv2.multiply(hsv[:, :, 1], self._saturation_boost)
            out = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)

        # Guarantee the caller's buffer is never handed back aliased.
        return out if out is not bgr else bgr.copy()
