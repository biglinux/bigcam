"""Camera profiles — named control presets, saved per camera.

Beyond letting people store their own settings, this ships two built-in
presets, because there is no single best configuration for a webcam: the right
answer depends on the light in the room, and the trade-off is real.  Measured
on an ASUS FHD webcam in a dim room, at 1280x720:

    exposure_dynamic_framerate=1   20 fps, correctly exposed
    exposure_dynamic_framerate=0   30 fps, badly underexposed

Letting the sensor lengthen its exposure collects real photons, which beats
any amount of correction applied afterwards — gain and gamma can only amplify
what was captured, noise included.  Giving that up buys smoother motion, which
is the better trade when the room is well lit.

Presenting both as named choices is deliberate.  An earlier attempt to pick
automatically produced a monochrome, milky picture, because whatever metric it
optimised was not what the person in front of the camera was looking at.

Controls live on the device, so switching profile changes what *every*
application sees, not just BigCam.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile

from core.camera_backend import CameraControl, CameraInfo
from utils import xdg
from utils.i18n import _

log = logging.getLogger(__name__)

# Built-in preset names.  Stored untranslated so a change of language does not
# orphan a user's saved selection; the UI translates them for display.
PRESET_QUALITY = "Quality"
PRESET_SMOOTH = "Smooth"
PRESET_FACTORY = "Factory defaults"  # spaces survive: see write_profile

# Human-readable descriptions, for the UI.
PRESET_DESCRIPTIONS = {
    PRESET_QUALITY: _(
        "Lets the camera use a longer exposure. Brighter and cleaner in dim "
        "rooms, at a lower frame rate."
    ),
    PRESET_SMOOTH: _(
        "Holds the full frame rate for smoother motion. Needs good lighting."
    ),
    PRESET_FACTORY: _("The camera's own default values."),
}

# Controls worth storing in a profile.  Everything else is read-only,
# transient, or derived, and writing it back would be noise.
PROFILE_CONTROLS = (
    "brightness",
    "contrast",
    "saturation",
    "gamma",
    "gain",
    "sharpness",
    "hue",
    "backlight_compensation",
    "power_line_frequency",
    "white_balance_automatic",
    "white_balance_temperature",
    "auto_exposure",
    "exposure_time_absolute",
    "exposure_dynamic_framerate",
)

# What each built-in preset overrides.  Anything not listed keeps the driver
# default, and anything the camera does not expose is skipped.
class _Scaled:
    """An override expressed relative to the driver's own default.

    An absolute number cannot be portable: this camera's gamma runs 72..500
    with a default of 100, another's runs 1..10.  Scaling the default and
    clamping to the control's range gives the same intent on both.
    """

    __slots__ = ("factor",)

    def __init__(self, factor: float) -> None:
        self.factor = factor

    def resolve(self, ctrl) -> int:
        value = int(round(int(ctrl.default) * self.factor))
        if ctrl.minimum is not None:
            value = max(int(ctrl.minimum), value)
        if ctrl.maximum is not None:
            value = min(int(ctrl.maximum), value)
        return value


_PRESET_OVERRIDES: dict[str, dict[str, object]] = {
    PRESET_QUALITY: {
        "exposure_dynamic_framerate": 1,   # allow a longer exposure
        "auto_exposure": 3,                # aperture priority
        "white_balance_automatic": 1,
        "gain": 0,                         # gain drains chroma on many sensors
    },
    PRESET_SMOOTH: {
        "exposure_dynamic_framerate": 0,   # never drop below the nominal rate
        "auto_exposure": 3,
        "white_balance_automatic": 1,
        # Locking the frame rate caps the exposure time, so the picture comes
        # out darker than Quality.  Lift it back with gamma rather than gain:
        # gain drains chroma badly on these sensors.
        "gamma": _Scaled(1.4),
        "gain": 0,
    },
    PRESET_FACTORY: {},
}

_LAST_USED_FILE = "last-used.json"

# USB ids are validated before going into a udev rule file.
_HEX_ID = re.compile(r"^[0-9a-fA-F]{4}$")


def _safe_filename(name: str) -> str:
    """Squash a name into something that cannot escape its directory."""
    cleaned = re.sub(r"[^\w\-.]", "_", name).strip("._")
    return cleaned or "unnamed"


def _camera_dir(camera: CameraInfo) -> str:
    path = os.path.join(xdg.profiles_dir(), _safe_filename(camera.name))
    os.makedirs(path, exist_ok=True)
    return path


def _profile_path(camera: CameraInfo, profile_name: str) -> str:
    return os.path.join(_camera_dir(camera), f"{_safe_filename(profile_name)}.json")


def builtin_names() -> list[str]:
    """The built-in preset names, in the order they should be offered."""
    return [PRESET_QUALITY, PRESET_SMOOTH, PRESET_FACTORY]


def display_name(profile_name: str) -> str:
    """Translated label for a profile, for the UI."""
    return {
        PRESET_QUALITY: _("Quality"),
        PRESET_SMOOTH: _("Smooth"),
        PRESET_FACTORY: _("Factory defaults"),
    }.get(profile_name, profile_name)


# -- reading and writing ---------------------------------------------------


def list_profiles(camera: CameraInfo) -> list[str]:
    """Return profile names available for *camera*, as the user typed them."""
    cam_dir = os.path.join(xdg.profiles_dir(), _safe_filename(camera.name))
    if not os.path.isdir(cam_dir):
        return []
    names: list[str] = []
    for filename in sorted(os.listdir(cam_dir)):
        if not filename.endswith(".json") or filename == _LAST_USED_FILE:
            continue
        # The filename is sanitised, so "Factory defaults" would come back as
        # "Factory_defaults".  The real name is stored inside the file.
        stored = _read_raw(os.path.join(cam_dir, filename))
        name = stored.get("name") if isinstance(stored, dict) else None
        names.append(name if isinstance(name, str) else filename[:-5])
    return names


def collect_settings(controls: list[CameraControl]) -> dict[str, int]:
    """Pick the storable, writable, integer controls out of *controls*."""
    out: dict[str, int] = {}
    for ctrl in controls or ():
        if ctrl.id not in PROFILE_CONTROLS:
            continue
        if ctrl.flags in ("read-only", "inactive"):
            continue
        try:
            out[ctrl.id] = int(ctrl.value)
        except (TypeError, ValueError):
            continue
    return out


def _read_raw(path: str) -> dict:
    """Load a profile file, tolerating anything unreadable."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_profile(
    camera: CameraInfo, profile_name: str, settings: dict[str, int]
) -> str:
    """Persist an explicit ``{control: value}`` mapping."""
    path = _profile_path(camera, profile_name)
    payload = {"name": profile_name, "settings": settings}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return path


def save_profile(
    camera: CameraInfo, profile_name: str, controls: list[CameraControl]
) -> str:
    """Persist the current values of *controls*.  Returns the file path."""
    return write_profile(camera, profile_name, collect_settings(controls))


def load_profile(camera: CameraInfo, profile_name: str) -> dict[str, int]:
    """Return saved values as ``{control_id: value}``, empty if unreadable."""
    raw = _read_raw(_profile_path(camera, profile_name))
    # Profiles written before the display name was stored are a flat mapping.
    settings = raw.get("settings") if "settings" in raw else raw
    if not isinstance(settings, dict):
        return {}
    return {
        name: value for name, value in settings.items()
        if isinstance(value, int) and not isinstance(value, bool)
    }


def delete_profile(camera: CameraInfo, profile_name: str) -> bool:
    path = _profile_path(camera, profile_name)
    if not os.path.isfile(path):
        return False
    os.remove(path)
    if last_profile(camera) == profile_name:
        remember_profile(camera, None)
    return True


# -- built-in presets ------------------------------------------------------


def ensure_builtin_profiles(
    camera: CameraInfo, controls: list[CameraControl]
) -> list[str]:
    """Create any missing built-in preset for *camera*.

    Each preset starts from the driver's own defaults and applies only the
    overrides that this camera actually exposes, so a device without, say,
    ``exposure_dynamic_framerate`` simply gets a preset without it rather than
    a write that fails.

    Presets already on disk are left alone: they are ordinary profiles once
    created, and the user may have edited them.
    """
    available = {
        ctrl.id: ctrl for ctrl in controls or ()
        if ctrl.id in PROFILE_CONTROLS and ctrl.flags not in ("read-only", "inactive")
    }
    if not available:
        return []

    defaults: dict[str, int] = {}
    for cid, ctrl in available.items():
        try:
            defaults[cid] = int(ctrl.default)
        except (TypeError, ValueError):
            continue

    created: list[str] = []
    existing = set(list_profiles(camera))
    for name in builtin_names():
        if name in existing:
            continue
        settings = dict(defaults)
        for cid, value in _PRESET_OVERRIDES[name].items():
            if cid not in available:
                continue
            if isinstance(value, _Scaled):
                value = value.resolve(available[cid])
            settings[cid] = int(value)

        # A preset whose every value matches the driver's defaults is the
        # factory preset under another name.  Offering it as a separate
        # choice is a promise the camera cannot keep: Smooth shipped setting
        # the three controls this webcam already defaults to, so picking it
        # changed nothing and looked like a bug.
        if name != PRESET_FACTORY and settings == defaults:
            log.info(
                "Skipping built-in profile %r for %s: identical to the "
                "driver defaults on this camera", name, camera.name,
            )
            continue

        write_profile(camera, name, settings)
        created.append(name)
    if created:
        log.info("Created built-in profiles for %s: %s", camera.name, created)
    return created


# -- applying --------------------------------------------------------------


def apply_profile(manager, camera: CameraInfo, profile_name: str) -> int:
    """Write a saved profile onto the device.  Returns how many controls took.

    A camera that rejects one control must not abort the rest, so each write
    is isolated.
    """
    settings = load_profile(camera, profile_name)
    if not settings:
        return 0
    applied = 0
    for name, value in settings.items():
        try:
            if manager.set_control(camera, name, value):
                applied += 1
        except Exception:
            log.debug("Could not set %s on %s", name, camera.name, exc_info=True)
    if applied:
        log.info(
            "Applied profile %r to %s (%d controls)", profile_name, camera.name, applied
        )
    return applied


# -- remembering the choice ------------------------------------------------


def _last_used_path(camera: CameraInfo) -> str:
    return os.path.join(_camera_dir(camera), _LAST_USED_FILE)


def remember_profile(camera: CameraInfo, profile_name: str | None) -> None:
    """Record which profile is active, so it can be restored next time."""
    path = _last_used_path(camera)
    if profile_name is None:
        if os.path.exists(path):
            os.remove(path)
        return
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"profile": profile_name}, fh)


def last_profile(camera: CameraInfo) -> str | None:
    """The profile last applied to *camera*, if it still exists."""
    path = _last_used_path(camera)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            name = json.load(fh).get("profile")
    except (OSError, ValueError):
        return None
    if not isinstance(name, str):
        return None
    # A profile deleted behind our back must not linger as a dangling choice.
    return name if name in list_profiles(camera) else None


# -- surviving a replug ----------------------------------------------------

def format_restore_command(device: str, settings: dict[str, int]) -> list[str]:
    """Build the argv that reapplies *settings* to *device* in one call."""
    if not settings:
        return []
    cmd = ["v4l2-ctl", "-d", device]
    for name, value in sorted(settings.items()):
        cmd += ["--set-ctrl", f"{name}={int(value)}"]
    return cmd


def build_udev_rule(vendor: str, product: str, settings: dict[str, int]) -> str:
    """Generate a udev rule reapplying *settings* when the camera appears.

    Scoped to one USB vendor:product pair and to capture-capable nodes, so it
    cannot touch another camera or a metadata node.  IDs are validated as
    4-digit hex because they end up inside a rule file.
    """
    if not settings:
        return ""
    for label, value in (("vendor", vendor), ("product", product)):
        if not _HEX_ID.match(value or ""):
            raise ValueError(f"invalid USB {label} id: {value!r}")

    assignments = " ".join(
        f"--set-ctrl {name}={int(value)}" for name, value in sorted(settings.items())
    )
    return f"""\
# BigCam — restore tuned controls for USB camera {vendor}:{product}.
#
# V4L2 controls reset to the driver's defaults whenever the device
# re-enumerates.  This reapplies BigCam's tuning at plug time, so every
# application sees the tuned picture without BigCam having to be running.
#
# Install with:
#   sudo cp bigcam-camera-tuning.rules /etc/udev/rules.d/
#   sudo udevadm control --reload
#
ACTION=="add", SUBSYSTEM=="video4linux", \\
  ATTRS{{idVendor}}=="{vendor}", ATTRS{{idProduct}}=="{product}", \\
  ENV{{ID_V4L_CAPABILITIES}}==":capture:", \\
  RUN+="/usr/bin/v4l2-ctl -d /dev/%k {assignments}"
"""
