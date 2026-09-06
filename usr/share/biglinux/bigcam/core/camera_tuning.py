"""Auto-tuning of the camera's own V4L2 controls.

:mod:`core.auto_enhance` fixes pixels after capture.  This module fixes the
capture itself, which is strictly better when it is possible: correcting
exposure at the sensor costs no CPU and amplifies no noise, whereas brightening
a dark frame in software amplifies whatever noise the sensor recorded.

The interesting decision is *how* to gather more light.  UVC cameras offer two
routes and pick badly on their own.  Measured on a Logitech-class webcam in a
dimly lit room, at 1280x720 MJPEG:

===========================  ========  ======  =======  =========
setting                      fps       luma    noise    sharpness
===========================  ========  ======  =======  =========
factory defaults              29.5      25      1.20      5.1
``exposure_dynamic_framerate``  19.7    66      1.92     11.9
gain 70 + brightness 10       29.6      62      1.82     16.4
===========================  ========  ======  =======  =========

Letting the camera drop its framerate was worse on *every* axis than raising
analog gain to the same brightness — half the framerate, more noise and less
detail.  So the tuner turns that behaviour off and uses gain instead, falling
back to the brightness offset only once gain is exhausted.

The module is deliberately split into pure decision functions and a thin
applier, so the policy can be tested without a camera.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Same target as the software enhancer, so the two agree on what "correctly
# exposed" means and do not fight each other.
TARGET_LUMA = 120.0

# Dead-band around the target.  Without it the loop hunts forever, visibly
# pumping the exposure.
_LUMA_TOLERANCE = 12.0

# Fraction of the remaining error corrected per step.  Well under 1.0 so the
# loop approaches the target instead of overshooting and oscillating.
_STEP_FACTOR = 0.35

# Smallest change worth issuing: each one is a v4l2-ctl subprocess.
_MIN_STEP = 2

# V4L2 menu value for aperture-priority auto exposure.
_AUTO_EXPOSURE_APERTURE = 3

# Stop raising gain once mean chroma saturation has fallen to this fraction
# of what the sensor produced at its lowest gain.  Past that the sensor is
# trading colour for brightness, and software gamma does the same job
# without touching chroma.
_MIN_SATURATION_RATIO = 0.75


@dataclass(frozen=True)
class ControlSpec:
    """A V4L2 control as reported by the backend."""

    name: str
    value: int
    minimum: int = 0
    maximum: int = 100
    default: int = 0
    flags: str = ""

    @property
    def writable(self) -> bool:
        return self.flags not in ("read-only", "inactive")


@dataclass
class TuningPlan:
    """A set of one-shot control changes, with a human-readable rationale."""

    changes: dict[str, int] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def add(self, name: str, value: int, reason: str) -> None:
        self.changes[name] = value
        self.reasons.append(reason)

    def describe(self) -> str:
        """One line per change, for the UI and the log."""
        if not self.reasons:
            return "camera already configured"
        return "; ".join(self.reasons)


def plan_initial_setup(
    controls: dict[str, ControlSpec], mains_hz: int = 50
) -> TuningPlan:
    """Decide the one-shot configuration changes for *controls*.

    Only returns entries that the camera actually exposes, that are writable,
    and whose value would really change — so applying an already-tuned camera
    is a no-op rather than a burst of pointless subprocess calls.
    """
    plan = TuningPlan()

    def want(name: str, value: int, reason: str) -> None:
        spec = controls.get(name)
        if spec is None or not spec.writable or spec.value == value:
            return
        if not (spec.minimum <= value <= spec.maximum):
            return
        plan.add(name, value, reason)

    want(
        "auto_exposure", _AUTO_EXPOSURE_APERTURE,
        "enabled automatic exposure",
    )
    want(
        "white_balance_automatic", 1,
        "enabled automatic white balance",
    )
    # The measured reason this module exists.
    want(
        "exposure_dynamic_framerate", 0,
        "stopped trading framerate for brightness (gain is cheaper)",
    )
    want(
        "power_line_frequency", 2 if mains_hz == 60 else 1,
        f"set anti-flicker for {mains_hz} Hz mains",
    )
    return plan


def next_adjustment(
    controls: dict[str, ControlSpec],
    median_luma: float,
    saturation: float | None = None,
    reference_saturation: float | None = None,
) -> tuple[str, int] | None:
    """Return the next ``(control, value)`` to apply, or None when settled.

    Called in a slow closed loop: apply one change, let the sensor react, then
    measure again.

    Gain is applied in the analog domain, before digitisation, so it is the
    right first choice — but only up to a point.  Many UVC sensors denoise
    chroma harder as gain rises, and past a certain level the picture drains
    to grey.  Measured on an ASUS FHD webcam in a dim room:

        gain    0   luma 24   saturation 15.1   noise 1.21
        gain   50   luma 43   saturation  9.5   noise 1.75
        gain  100   luma 63   saturation  6.3   noise 2.16

    Brightening the *gain 0* frame in software reached luma 96 while keeping
    saturation at 15.7 — four times the colour of the gain-100 frame, at half
    the noise.  So when *saturation* is supplied and the sensor is visibly
    draining colour, this stops pushing gain and leaves the rest of the
    exposure deficit to :mod:`core.auto_enhance`, which cannot damage chroma.

    Without a *saturation* measurement the guard is inactive and the loop
    behaves as a plain luminance servo.
    """
    error = TARGET_LUMA - median_luma
    if abs(error) <= _LUMA_TOLERANCE:
        return None

    colour_is_draining = (
        error > 0
        and saturation is not None
        and reference_saturation is not None
        and reference_saturation > 0.0
        and saturation < reference_saturation * _MIN_SATURATION_RATIO
    )
    if colour_is_draining:
        log.info(
            "Auto-tune: stopping gain at saturation %.1f (was %.1f) — "
            "software correction preserves colour better",
            saturation, reference_saturation,
        )
        return None

    for name, scale in (("gain", 1.1), ("brightness", 1.4)):
        spec = controls.get(name)
        if spec is None or not spec.writable:
            continue

        # How far this control would have to move to close the error, damped
        # so the loop converges instead of ringing.
        delta = int(round(error / scale * _STEP_FACTOR))
        if abs(delta) < _MIN_STEP:
            delta = _MIN_STEP if error > 0 else -_MIN_STEP

        target = max(spec.minimum, min(spec.maximum, spec.value + delta))
        if target != spec.value:
            return name, int(target)
        # This control is pinned at the end of its range; try the next one.

    return None


def specs_from_controls(controls) -> dict[str, ControlSpec]:
    """Adapt a backend's ``CameraControl`` list into :class:`ControlSpec`s."""
    out: dict[str, ControlSpec] = {}
    for ctrl in controls or ():
        try:
            value = int(ctrl.value)
        except (TypeError, ValueError):
            continue
        out[ctrl.id] = ControlSpec(
            name=ctrl.id,
            value=value,
            minimum=int(ctrl.minimum) if ctrl.minimum is not None else 0,
            maximum=int(ctrl.maximum) if ctrl.maximum is not None else 100,
            default=int(ctrl.default) if isinstance(ctrl.default, int) else 0,
            flags=ctrl.flags or "",
        )
    return out


def detect_mains_hz() -> int:
    """Best-effort mains frequency, for anti-flicker.

    The Americas run at 60 Hz, most of the rest of the world at 50 Hz.  Derived
    from the system timezone, which is the only hint available offline.
    """
    import os

    try:
        region = os.readlink("/etc/localtime").split("/zoneinfo/")[-1].split("/")[0]
        if region == "America":
            return 60
    except OSError:
        pass
    if os.environ.get("TZ", "").startswith("America/"):
        return 60
    return 50
