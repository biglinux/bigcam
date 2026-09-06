"""Motion-adaptive temporal denoising.

A webcam sensor is small, so in dim light most of what it records is noise.
Brightening that in software makes the noise more visible, which is why the
exposure correction in :mod:`core.auto_enhance` has to hold back.  Removing
the noise first is what lets it push further.

Sensor noise is random from frame to frame while the scene is not, so
averaging successive frames cancels it.  This is the same trick a phone camera
uses to beat its own small sensor.  Measured on an ASUS FHD webcam:

    1 frame    noise 1.59
    2 frames   noise 0.97   (1.6x better)
    4 frames   noise 0.64   (2.5x better)
    8 frames   noise 0.41   (3.9x better)

Buffering N frames and averaging them would be the obvious implementation and
is the wrong one for a live preview: eight frames is 267 ms of latency at
30 fps, and anything that moves during those frames smears.

So this keeps a single running average and blends each new frame into it
*per pixel*, choosing the blend factor from how much that pixel changed:

* a pixel that barely moved is mostly noise, so it is averaged heavily;
* a pixel that changed a lot is real motion, so it passes straight through.

No buffer, no latency, and a moving hand stays sharp while the wall behind it
is cleaned up.
"""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)

try:
    import cv2

    _HAS_CV2 = True
except ImportError:  # pragma: no cover
    _HAS_CV2 = False

# Per-pixel difference, in levels, at which a pixel is considered to be moving
# rather than merely noisy.  Sensor noise on this class of camera sits around
# 1.5-2 levels, so a threshold in the mid teens separates the two with room to
# spare while still catching slow movement.
_MOTION_KNEE = 14.0

# Blend factor for a pixel judged completely static.  0.12 means the running
# average keeps 88% of its history, roughly equivalent to averaging 8 frames.
_STATIC_ALPHA = 0.12

# Mean absolute change over the whole frame that counts as a scene cut — a
# camera switch, a light turned on. History is dropped rather than blended, so
# the new scene is not haunted by the old one.
_SCENE_CUT_DELTA = 40.0

# Longest side of the reduced-resolution motion mask.  The mask is blurred and
# then upscaled, so extra detail here buys nothing.
_MASK_MAX_DIM = 320


class TemporalDenoiser:
    """Recursive, motion-adaptive frame averaging.

    Stateful: create one per stream and :meth:`reset` when the source changes.
    *strength* scales how aggressively static areas are averaged; 0 disables
    the filter entirely.
    """

    def __init__(self, strength: float = 1.0) -> None:
        self._strength = float(np.clip(strength, 0.0, 1.0))
        self._average: np.ndarray | None = None

    @property
    def strength(self) -> float:
        return self._strength

    def set_strength(self, value: float) -> None:
        self._strength = float(np.clip(value, 0.0, 1.0))

    def reset(self) -> None:
        """Forget the running average; the next frame starts fresh."""
        self._average = None

    def process(self, bgr: np.ndarray) -> np.ndarray:
        """Return a denoised copy of *bgr*.  The input is never modified."""
        if not _HAS_CV2 or self._strength <= 0.0:
            return bgr
        if bgr is None or bgr.ndim != 3 or bgr.shape[2] != 3:
            return bgr
        if bgr.dtype != np.uint8:
            return bgr

        current = bgr.astype(np.float32)

        # First frame, a resolution change, or a reset: nothing to blend with.
        if self._average is None or self._average.shape != current.shape:
            self._average = current
            return bgr

        try:
            return self._blend(bgr, current)
        except cv2.error:
            log.debug("Temporal denoise failed on this frame", exc_info=True)
            self._average = current
            return bgr

    def _motion_mask(self, current: np.ndarray) -> tuple[np.ndarray, float]:
        """Per-pixel blend factor, and the mean change that produced it.

        Built at reduced resolution: a motion mask is inherently low frequency
        and gets blurred anyway, so computing it on the full frame is wasted
        work.  On a 1080p frame this is the difference between ~55 ms and a
        few, which decides whether the filter is usable in a live preview.
        """
        h, w = current.shape[:2]
        step = max(1, max(h, w) // _MASK_MAX_DIM)
        small_cur = current[::step, ::step]
        small_avg = self._average[::step, ::step]

        diff = cv2.absdiff(small_cur, small_avg)
        mean_delta = float(diff.mean())

        # Judge motion on the mean across channels: chroma is the noisiest
        # channel in low light and should not on its own look like movement.
        motion = diff.mean(axis=2)
        # Soften it, or the border between "moving" and "still" shows up as a
        # visible outline around the subject.
        motion = cv2.GaussianBlur(motion, (0, 0), 1.5)

        static_alpha = _STATIC_ALPHA + (1.0 - _STATIC_ALPHA) * (1.0 - self._strength)
        alpha = static_alpha + (1.0 - static_alpha) * np.clip(
            motion * (1.0 / _MOTION_KNEE), 0.0, 1.0
        )
        if step > 1:
            alpha = cv2.resize(alpha, (w, h), interpolation=cv2.INTER_LINEAR)
        return alpha.astype(np.float32), mean_delta

    def _blend(self, original: np.ndarray, current: np.ndarray) -> np.ndarray:
        alpha, mean_delta = self._motion_mask(current)

        # A hard cut invalidates the whole history; blending would ghost.
        if mean_delta > _SCENE_CUT_DELTA:
            self._average = current
            return original

        # Written as  average += alpha * (current - average)  rather than the
        # textbook  current*alpha + average*(1-alpha).  Algebraically the same,
        # but three full-resolution passes instead of six, which at 1080p is
        # the difference between fitting in a 30 fps budget and not.
        alpha3 = cv2.merge([alpha, alpha, alpha])
        delta = cv2.subtract(current, self._average)
        self._average = cv2.add(self._average, cv2.multiply(delta, alpha3))
        return cv2.convertScaleAbs(self._average)
