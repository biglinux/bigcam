"""Every declared dependency must exist in a repository.

A package in the AUR cannot be a dependency of a repo package: makepkg in the
build container has no AUR, so `depends=('python-pyzbar')` fails the whole
build with

    ==> ERROR: Could not resolve all dependencies.

after the source has already been fetched.  Nothing in the source tree hints
that a name is AUR-only, so the mistake survives review and only surfaces in
CI minutes later.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PKGBUILD = REPO / "pkgbuild" / "PKGBUILD"


def _declared(array: str) -> list[str]:
    """Pull the quoted names out of a bash array in the PKGBUILD."""
    text = PKGBUILD.read_text()
    match = re.search(rf"^{array}=\((.*?)\)$", text, re.S | re.M)
    if not match:
        return []
    body = re.sub(r"#.*", "", match.group(1))
    return re.findall(r"'([^']+)'", body)


def _names() -> list[str]:
    return _declared("depends") + _declared("makedepends")


def test_dependencies_are_declared():
    assert _names(), "parsed no dependencies; the rest of this file proves nothing"


def test_no_duplicate_dependencies():
    names = _names()
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert dupes == [], f"declared more than once: {dupes}"


@pytest.mark.slow
@pytest.mark.parametrize("pkg", _names())
def test_dependency_is_available_in_a_repository(pkg):
    if not shutil.which("pacman"):
        pytest.skip("pacman not available")
    # -Sp, not -Si: a dependency may be satisfied by a *provider* rather than
    # by a package of that name.  linux-headers is provided by whichever
    # linux<ver>-headers is installed, and -Si does not resolve provides.
    res = subprocess.run(
        ["pacman", "-Sp", "--print-format", "%n", "--noconfirm", pkg],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, (
        f"'{pkg}' is in no configured repository and nothing provides it — "
        f"if it is an AUR package it cannot be a dependency, and the build "
        f"will fail to resolve it"
    )
