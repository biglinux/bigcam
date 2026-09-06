"""Translations must compile, and must not squat on shared paths.

Two separate defects live here.

The first: /usr/share/locale is a namespace every package writes into.  A
compiled catalogue is namespaced by its text domain — pt_BR/LC_MESSAGES/
bigcam.mo can only ever be ours — but a translation *source* dropped flat at
/usr/share/locale/pt-BR.po names no project.  BigCam shipped 30 of them and
pacman refused to install the package at all:

    bigcam: /usr/share/locale/pt-BR.po exists in filesystem
            (owned by big-gnome-center)

The second: the .mo files used to be committed next to the .po they are built
from.  That gave one generated binary two producers — the translation bot and
whoever ran the build script — and git cannot three-way merge a binary, so
every push conflicted on usr/share/locale/en/LC_MESSAGES/bigcam.mo.  They are
generated now, by build-translations.sh and by the PKGBUILD, and are not
tracked.  So these tests build them rather than assuming they are present.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "locale"
INSTALLED = REPO / "usr" / "share" / "locale"
SCRIPT = REPO / "build-translations.sh"
DOMAIN = "bigcam"


def _tags() -> list[str]:
    return sorted(p.stem for p in SRC.glob("*.po"))


@pytest.fixture(scope="session")
def built() -> Path:
    """Run the real build script; skip everything if gettext is missing."""
    if not shutil.which("msgfmt"):
        pytest.skip("gettext not installed")
    res = subprocess.run(
        [str(SCRIPT)], capture_output=True, text=True, cwd=REPO
    )
    assert res.returncode == 0, f"build-translations.sh failed:\n{res.stderr}"
    return INSTALLED


def test_there_are_translations_to_check():
    assert _tags(), "no .po files found; the rest of this file proves nothing"


# -- the packaging conflict ------------------------------------------------


def test_no_translation_sources_are_installed(built):
    stray = sorted(
        p.relative_to(REPO).as_posix()
        for p in built.rglob("*")
        if p.suffix in (".po", ".pot")
    )
    assert stray == [], (
        f"{len(stray)} translation source(s) under usr/: {stray[:3]}... "
        f"these collide with other packages in /usr/share/locale"
    )


def test_everything_installed_is_a_namespaced_catalogue(built):
    for path in built.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(built)
        assert rel.parts[-2:] == ("LC_MESSAGES", f"{DOMAIN}.mo"), (
            f"{rel} is not <lang>/LC_MESSAGES/{DOMAIN}.mo — a path that is "
            f"not namespaced by the text domain can conflict"
        )


# -- the catalogues gettext will actually look for -------------------------


@pytest.mark.parametrize("tag", _tags())
def test_every_language_is_built(tag, built):
    """gettext resolves the POSIX locale name, so pt-BR must land as pt_BR."""
    expected = built / tag.replace("-", "_") / "LC_MESSAGES" / f"{DOMAIN}.mo"
    assert expected.is_file(), f"{tag}.po produced no catalogue at {expected}"
    assert expected.stat().st_size > 0


def test_no_catalogue_uses_a_hyphenated_language_name(built):
    """A pt-BR/ directory is never consulted; it silently goes stale."""
    bad = [d.name for d in built.iterdir() if d.is_dir() and "-" in d.name]
    assert bad == [], f"gettext will never read {bad}; use an underscore"


def test_no_catalogue_without_a_source(built):
    orphans = [
        d.name
        for d in built.iterdir()
        if d.is_dir()
        and not (SRC / f"{d.name.replace('_', '-')}.po").is_file()
        and not (SRC / f"{d.name}.po").is_file()
    ]
    assert orphans == [], f"catalogues with no .po to rebuild from: {orphans}"


# -- the generated files must stay out of git ------------------------------


def test_compiled_catalogues_are_not_tracked():
    """Two producers plus a binary format means a conflict on every push."""
    res = subprocess.run(
        ["git", "ls-files", "usr/share/locale"],
        capture_output=True, text=True, cwd=REPO,
    )
    if res.returncode != 0:
        pytest.skip("not a git checkout")
    tracked = [line for line in res.stdout.split() if line]
    assert tracked == [], (
        f"{len(tracked)} generated file(s) tracked under usr/share/locale: "
        f"{tracked[:3]}... these conflict on every merge"
    )


# -- the build script ------------------------------------------------------


def test_build_script_is_executable():
    assert SCRIPT.is_file()
    assert SCRIPT.stat().st_mode & 0o111, "not executable"


def test_pkgbuild_compiles_translations():
    """A stale checkout must not be able to ship a stale catalogue."""
    text = (REPO / "pkgbuild" / "PKGBUILD").read_text()
    assert "build-translations.sh" in text, (
        "package() does not compile the catalogues, so whatever .mo happens "
        "to be in the tree gets shipped"
    )


@pytest.mark.slow
def test_every_source_compiles():
    """A .po with a broken format string would ship a half-empty catalogue."""
    if not shutil.which("msgfmt"):
        pytest.skip("gettext not installed")
    for po in SRC.glob("*.po"):
        res = subprocess.run(
            ["msgfmt", "--check-format", "-o", "/dev/null", str(po)],
            capture_output=True, text=True,
        )
        assert res.returncode == 0, f"{po.name} does not compile:\n{res.stderr}"
