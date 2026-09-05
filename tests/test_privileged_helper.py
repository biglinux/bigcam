"""The privileged v4l2loopback helper and the sudoers rule that grants it.

This is the only code BigCam runs as root, so its argument validation is a
security boundary: everything else assumes it cannot be aimed at an arbitrary
module, an arbitrary file, or a physical camera node.
"""

from __future__ import annotations

import os
import re
import subprocess

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
HELPER = os.path.join(
    REPO, "usr", "share", "biglinux", "bigcam", "script", "bigcam-v4l2loopback"
)
SUDOERS = os.path.join(REPO, "etc", "sudoers.d", "bigcam")
MODPROBE_CONF = os.path.join(REPO, "etc", "modprobe.d", "v4l2loopback.conf")


def _run(*args, env=None):
    """Run the helper with modprobe/v4l2loopback-ctl shadowed by stubs."""
    return subprocess.run(
        [HELPER, *args],
        capture_output=True,
        text=True,
        timeout=15,
        env=env or {**os.environ, "PATH": "/nonexistent"},
    )


# -- packaging -------------------------------------------------------------


def test_helper_exists_and_is_executable():
    assert os.path.isfile(HELPER)
    assert os.access(HELPER, os.X_OK), "helper must ship with the exec bit set"


def test_helper_passes_shellcheck_syntax():
    proc = subprocess.run(["bash", "-n", HELPER], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


# -- argument validation ---------------------------------------------------


@pytest.mark.parametrize("args", [
    (),
    ("bogus",),
    ("load", "extra"),
    ("unload", "extra"),
    ("add",),
    ("add", "label"),
    ("delete",),
    ("delete", "/dev/video20", "extra"),
])
def test_malformed_invocations_are_rejected(args):
    proc = _run(*args)
    assert proc.returncode == 2, f"{args} should be rejected, got rc={proc.returncode}"


@pytest.mark.parametrize("device", [
    "/dev/video0",          # a real webcam
    "/dev/video1",
    "/dev/video19",         # just below the pool
    "/dev/video28",         # just above the pool
    "/dev/sda",
    "/etc/passwd",
    "../../etc/passwd",
    "/dev/video20; rm -rf /",
    "/dev/video20 /dev/video0",
    "",
])
def test_devices_outside_the_pool_are_rejected(device):
    proc = _run("delete", device)
    assert proc.returncode == 2, (
        f"helper accepted {device!r} — it must only touch the BigCam pool"
    )


@pytest.mark.parametrize("device", [f"/dev/video{n}" for n in range(20, 28)])
def test_devices_inside_the_pool_pass_validation(device):
    """Validation must accept the pool; failure past that point is fine here."""
    proc = _run("delete", device)
    assert proc.returncode != 2, f"{device} should pass argument validation"


@pytest.mark.parametrize("label", [
    "Bad;Label",
    "Label$(id)",
    "Label`id`",
    "Label\nsecond",
    "Label|pipe",
    "../escape",
    "",
    "x" * 33,
])
def test_dangerous_labels_are_rejected(label):
    proc = _run("add", label, "/dev/video20")
    assert proc.returncode == 2, f"helper accepted label {label!r}"


@pytest.mark.parametrize("label", ["BigCam Virtual 1", "Webcam_2", "cam-3"])
def test_reasonable_labels_pass_validation(label):
    proc = _run("add", label, "/dev/video20")
    assert proc.returncode != 2, f"label {label!r} should be accepted"


def test_helper_never_forwards_extra_modprobe_flags():
    """The old wildcard sudoers rule allowed `modprobe v4l2loopback -C ...`."""
    src = open(HELPER, encoding="utf-8").read()
    # modprobe is only ever invoked with a literal module name and k=v params.
    for match in re.finditer(r'"\$MODPROBE"([^\n]*)', src):
        line = match.group(1)
        assert '"$@"' not in line, "helper forwards raw user arguments to modprobe"
        assert "$1" not in line and "$2" not in line, (
            f"helper interpolates a positional arg into modprobe: {line.strip()}"
        )


# -- sudoers rule ----------------------------------------------------------


def _sudoers_rules() -> list[str]:
    """Active (non-comment, non-blank) lines of the sudoers drop-in."""
    return [
        ln.strip() for ln in open(SUDOERS, encoding="utf-8")
        if ln.strip() and not ln.strip().startswith("#")
    ]


def test_sudoers_grants_only_the_helper():
    rules = _sudoers_rules()
    cmnd_rules = [r for r in rules if "NOPASSWD" in r]
    assert len(cmnd_rules) == 1, f"expected exactly one NOPASSWD rule, got {cmnd_rules}"

    granted = "\n".join(rules)
    assert "modprobe" not in granted, (
        "sudoers must not grant modprobe directly — use the validating helper"
    )
    assert "v4l2loopback-ctl" not in granted, (
        "sudoers must not grant v4l2loopback-ctl directly"
    )
    assert "bigcam-v4l2loopback" in granted


def test_sudoers_has_no_wildcards():
    """`*` in a sudoers command matches spaces and slashes: it grants too much."""
    for rule in _sudoers_rules():
        assert "*" not in rule, f"wildcard reintroduced in sudoers: {rule}"


def test_sudoers_syntax_is_valid():
    visudo = "/usr/sbin/visudo"
    if not os.path.exists(visudo):
        pytest.skip("visudo not available")
    proc = subprocess.run(
        [visudo, "-c", "-f", SUDOERS], capture_output=True, text=True
    )
    assert proc.returncode == 0, f"invalid sudoers file:\n{proc.stdout}{proc.stderr}"


# -- the three device-pool definitions must agree -------------------------


def _helper_pool():
    src = open(HELPER, encoding="utf-8").read()
    base = int(re.search(r"readonly DEVICE_BASE=(\d+)", src).group(1))
    size = int(re.search(r"readonly MAX_DEVICES=(\d+)", src).group(1))
    return base, size


def test_python_and_helper_agree_on_the_pool():
    from core import virtual_camera as vc

    base, size = _helper_pool()
    assert (vc.DEVICE_BASE, vc.DEVICE_POOL_SIZE) == (base, size), (
        "virtual_camera.py and the helper disagree about the device pool"
    )


def _modprobe_options_line() -> str:
    for line in open(MODPROBE_CONF, encoding="utf-8"):
        if line.strip().startswith("options "):
            return line.strip()
    raise AssertionError("no active `options` line in modprobe.d config")


def test_modprobe_conf_agrees_on_the_pool():
    base, size = _helper_pool()
    conf = _modprobe_options_line()
    nrs = re.search(r"video_nr=([\d,]+)", conf).group(1).split(",")
    assert [int(n) for n in nrs] == list(range(base, base + size)), (
        "modprobe.d pool does not match the helper's DEVICE_BASE/MAX_DEVICES"
    )
    devices = int(re.search(r"devices=(\d+)", conf).group(1))
    assert devices == size


def test_no_stale_loader_script_remains():
    """load-v4l2loopback.sh was referenced by polkit but never invoked."""
    stale = os.path.join(
        REPO, "usr", "share", "biglinux", "bigcam", "script", "load-v4l2loopback.sh"
    )
    assert not os.path.exists(stale)


def test_polkit_points_at_the_helper():
    policy = os.path.join(
        REPO, "etc", "polkit-1", "actions", "br.com.biglinux.bigcam.policy"
    )
    content = open(policy, encoding="utf-8").read()
    assert "bigcam-v4l2loopback" in content
    assert "load-v4l2loopback.sh" not in content, "polkit references a deleted script"
