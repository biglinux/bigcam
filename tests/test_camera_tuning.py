"""Hardware auto-tuning of the camera's own V4L2 controls.

Software enhancement can only rescue what the sensor already captured.  Fixing
exposure at the camera is strictly better: it costs no CPU and amplifies no
noise.  Measured on a real webcam, the sensor's own "drop the framerate to
gather light" behaviour was worse on *every* axis than raising analog gain:

    dynamic framerate  19.7 fps  luma 66  noise 1.92  sharpness 11.9
    gain + brightness  29.6 fps  luma 62  noise 1.82  sharpness 16.4

So the tuner prefers gain, and only sacrifices framerate as a last resort.
"""

from __future__ import annotations


from core.camera_tuning import (
    ControlSpec,
    TuningPlan,
    next_adjustment,
    plan_initial_setup,
)


def _spec(name, value, minimum=0, maximum=100, default=0, flags=""):
    return ControlSpec(
        name=name, value=value, minimum=minimum, maximum=maximum,
        default=default, flags=flags,
    )


def _controls(**overrides):
    base = {
        "auto_exposure": _spec("auto_exposure", 1, 0, 3, 3),
        "white_balance_automatic": _spec("white_balance_automatic", 0, 0, 1, 1),
        "exposure_dynamic_framerate": _spec(
            "exposure_dynamic_framerate", 1, 0, 1, 0
        ),
        "gain": _spec("gain", 0, 0, 100, 0),
        "brightness": _spec("brightness", 0, -64, 64, 0),
        "power_line_frequency": _spec("power_line_frequency", 0, 0, 2, 2),
    }
    base.update(overrides)
    return base


# -- initial setup ---------------------------------------------------------


def test_initial_setup_enables_automatic_exposure():
    plan = plan_initial_setup(_controls(), mains_hz=60)
    assert plan.changes["auto_exposure"] == 3, "aperture priority not requested"


def test_initial_setup_enables_auto_white_balance():
    plan = plan_initial_setup(_controls(), mains_hz=60)
    assert plan.changes["white_balance_automatic"] == 1


def test_initial_setup_disables_dynamic_framerate():
    """Trading framerate for light measured worse than using gain."""
    plan = plan_initial_setup(_controls(), mains_hz=60)
    assert plan.changes["exposure_dynamic_framerate"] == 0


def test_initial_setup_sets_anti_flicker_from_mains():
    assert plan_initial_setup(_controls(), mains_hz=60).changes[
        "power_line_frequency"
    ] == 2
    assert plan_initial_setup(_controls(), mains_hz=50).changes[
        "power_line_frequency"
    ] == 1


def test_initial_setup_skips_controls_already_correct():
    already = _controls(
        auto_exposure=_spec("auto_exposure", 3, 0, 3, 3),
        power_line_frequency=_spec("power_line_frequency", 2, 0, 2, 2),
    )
    plan = plan_initial_setup(already, mains_hz=60)
    assert "auto_exposure" not in plan.changes
    assert "power_line_frequency" not in plan.changes


def test_initial_setup_ignores_controls_the_camera_lacks():
    plan = plan_initial_setup({"gain": _spec("gain", 0)}, mains_hz=60)
    assert plan.changes == {}


def test_read_only_controls_are_never_written():
    locked = _controls(
        gain=_spec("gain", 0, flags="read-only"),
        auto_exposure=_spec("auto_exposure", 1, 0, 3, 3, flags="inactive"),
    )
    plan = plan_initial_setup(locked, mains_hz=60)
    assert "auto_exposure" not in plan.changes


# -- closed-loop exposure --------------------------------------------------


def test_dark_frame_raises_gain():
    adj = next_adjustment(_controls(), median_luma=30.0)
    assert adj is not None
    name, value = adj
    assert name == "gain"
    assert value > 0


def test_gain_rises_gradually_not_to_the_maximum():
    """One big jump overshoots and looks like a flash."""
    adj = next_adjustment(_controls(), median_luma=30.0)
    _, value = adj
    assert value < 100, "gain slammed to maximum in a single step"


def test_bright_frame_lowers_gain():
    hot = _controls(gain=_spec("gain", 60, 0, 100, 0))
    adj = next_adjustment(hot, median_luma=200.0)
    assert adj is not None
    name, value = adj
    assert name == "gain"
    assert value < 60


def test_good_frame_needs_no_adjustment():
    assert next_adjustment(_controls(), median_luma=120.0) is None


def test_dead_band_prevents_hunting():
    """Small deviations must not produce an endless stream of tweaks."""
    for luma in (112.0, 120.0, 128.0):
        assert next_adjustment(_controls(), median_luma=luma) is None


def test_brightness_is_used_once_gain_is_exhausted():
    maxed = _controls(gain=_spec("gain", 100, 0, 100, 0))
    adj = next_adjustment(maxed, median_luma=40.0)
    assert adj is not None
    assert adj[0] == "brightness", "should fall back to brightness at max gain"


def test_gives_up_when_everything_is_exhausted():
    maxed = _controls(
        gain=_spec("gain", 100, 0, 100, 0),
        brightness=_spec("brightness", 64, -64, 64, 0),
    )
    assert next_adjustment(maxed, median_luma=40.0) is None


def test_camera_without_gain_uses_brightness():
    no_gain = {"brightness": _spec("brightness", 0, -64, 64, 0)}
    adj = next_adjustment(no_gain, median_luma=40.0)
    assert adj is not None and adj[0] == "brightness"


def test_camera_with_no_usable_control_returns_none():
    assert next_adjustment({}, median_luma=40.0) is None


def test_adjustment_respects_control_bounds():
    near_max = _controls(gain=_spec("gain", 95, 0, 100, 0))
    adj = next_adjustment(near_max, median_luma=20.0)
    assert adj is not None
    assert adj[1] <= 100


def test_adjustment_is_an_integer():
    adj = next_adjustment(_controls(), median_luma=30.0)
    assert isinstance(adj[1], int), "V4L2 controls take integers"


# -- convergence -----------------------------------------------------------


def test_loop_converges_on_a_simulated_camera():
    """Repeatedly applying the adjustment must settle, not oscillate."""
    controls = _controls()
    # Toy sensor: luminance rises roughly linearly with gain.
    def luma_for(gain, brightness):
        return 25.0 + gain * 1.1 + brightness * 1.4

    history = []
    for _ in range(40):
        luma = luma_for(controls["gain"].value, controls["brightness"].value)
        history.append(luma)
        adj = next_adjustment(controls, luma)
        if adj is None:
            break
        name, value = adj
        old = controls[name]
        controls[name] = _spec(name, value, old.minimum, old.maximum, old.default)

    assert adj is None, f"never converged; last luma {history[-1]:.0f}"
    assert abs(history[-1] - 120.0) < 15, f"settled at {history[-1]:.0f}, want ~120"


def test_plan_is_reported_for_the_ui():
    plan = plan_initial_setup(_controls(), mains_hz=60)
    assert isinstance(plan, TuningPlan)
    assert plan.describe(), "plan should be explainable to the user"
