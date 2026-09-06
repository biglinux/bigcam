"""Installed translation files must not squat on shared paths.

/usr/share/locale is a namespace every package on the system writes into.  A
compiled catalogue is namespaced by its text domain — pt_BR/LC_MESSAGES/
bigcam.mo can only ever be ours — but a translation *source* dropped flat at
/usr/share/locale/pt-BR.po claims a path with no project in its name.  BigCam
shipped 30 of them and pacman refused to install the package at all:

    bigcam: /usr/share/locale/pt-BR.po exists in filesystem
            (owned by big-gnome-center)

The sources live in locale/ and are not installed.  The translation tooling
writes there, so nothing stops it from writing under usr/ again by accident.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "locale"
INSTALLED = REPO / "usr" / "share" / "locale"
DOMAIN = "bigcam"


def _tags() -> list[str]:
    return sorted(p.stem for p in SRC.glob("*.po"))


def test_there_are_translations_to_check():
    assert _tags(), "no .po files found; the rest of this file proves nothing"


# -- the conflict itself ---------------------------------------------------


def test_no_translation_sources_are_installed():
    stray = sorted(
        p.relative_to(REPO).as_posix()
        for p in INSTALLED.rglob("*")
        if p.suffix in (".po", ".pot")
    )
    assert stray == [], (
        f"{len(stray)} translation source(s) under usr/: {stray[:3]}... "
        f"these collide with other packages in /usr/share/locale"
    )


def test_installed_files_are_all_namespaced_catalogues():
    for path in INSTALLED.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(INSTALLED)
        assert rel.parts[-2:] == ("LC_MESSAGES", f"{DOMAIN}.mo"), (
            f"{rel} is not <lang>/LC_MESSAGES/{DOMAIN}.mo — a path that is "
            f"not namespaced by the text domain can conflict"
        )


# -- the catalogues gettext will actually look for -------------------------


@pytest.mark.parametrize("tag", _tags())
def test_every_language_has_a_catalogue(tag):
    """gettext resolves the POSIX locale name, so pt-BR must land as pt_BR."""
    expected = INSTALLED / tag.replace("-", "_") / "LC_MESSAGES" / f"{DOMAIN}.mo"
    assert expected.is_file(), (
        f"{tag}.po has no compiled catalogue at "
        f"{expected.relative_to(REPO)} — run ./build-translations.sh"
    )


@pytest.mark.parametrize("tag", _tags())
def test_catalogue_is_up_to_date_with_its_source(tag, tmp_path):
    """The .mo is committed, so it can drift from the .po behind it.

    A merge did exactly that: it took the incoming locale/en.po and kept the
    local catalogue, leaving 16 strings compiled out.  Nothing noticed,
    because checking that the file merely exists always passes.
    """
    if not shutil.which("msgfmt"):
        pytest.skip("gettext not installed")

    po = SRC / f"{tag}.po"
    installed = INSTALLED / tag.replace("-", "_") / "LC_MESSAGES" / f"{DOMAIN}.mo"
    fresh = tmp_path / "fresh.mo"
    subprocess.run(
        ["msgfmt", "--check-format", "-o", str(fresh), str(po)], check=True
    )
    assert fresh.read_bytes() == installed.read_bytes(), (
        f"{installed.relative_to(REPO)} is stale relative to {po.name} — "
        f"run ./build-translations.sh"
    )


def test_no_catalogue_uses_a_hyphenated_language_name():
    """A pt-BR/ directory is never consulted; it silently goes stale."""
    bad = [d.name for d in INSTALLED.iterdir() if d.is_dir() and "-" in d.name]
    assert bad == [], f"gettext will never read {bad}; use an underscore"


def test_no_catalogue_without_a_source():
    orphans = [
        d.name
        for d in INSTALLED.iterdir()
        if d.is_dir() and not (SRC / f"{d.name.replace('_', '-')}.po").is_file()
        and not (SRC / f"{d.name}.po").is_file()
    ]
    assert orphans == [], f"catalogues with no .po to rebuild from: {orphans}"


# -- the build script ------------------------------------------------------


def test_build_script_is_executable():
    script = REPO / "build-translations.sh"
    assert script.is_file()
    assert script.stat().st_mode & 0o111, "not executable"


@pytest.mark.slow
def test_every_source_compiles():
    """A .po with a broken format string would ship a half-empty catalogue."""
    for po in SRC.glob("*.po"):
        res = subprocess.run(
            ["msgfmt", "--check-format", "-o", "/dev/null", str(po)],
            capture_output=True, text=True,
        )
        assert res.returncode == 0, f"{po.name} does not compile:\n{res.stderr}"
