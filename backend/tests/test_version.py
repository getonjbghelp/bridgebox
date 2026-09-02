"""The app's version and the label the Beta badge derives from it."""
from bridgebox import version as version_mod


def test_the_label_is_derived_not_stored():
    """"b1" is what the user sees, but a bare "b1" is not a valid PEP 440
    version and packaging tools reject it - so pyproject holds "0.1.0b1" and
    the label comes out of it. Storing both would let them drift."""
    assert version_mod.release_label("0.1.0b1") == "b1"
    assert version_mod.release_label("0.2.0b3") == "b3"
    assert version_mod.release_label("1.0.0rc2") == "rc2"


def test_a_final_release_has_no_label_so_the_badge_disappears():
    """The tooltip promises the icon goes away on release. That has to be a
    property of the version, not a line somebody remembers to delete."""
    assert version_mod.release_label("0.1.0") == ""
    assert version_mod.release_label("1.2.3") == ""


def test_pyproject_wins_over_installed_metadata(monkeypatch):
    """Measured, not assumed: `pip install -e .` records the version at install
    time, so an editable checkout kept reporting "0.1.0" while pyproject
    already said "0.1.0b1". The file the developer edits has to win.

    The contrast is forced rather than borrowed from whatever this checkout
    happens to have installed: the two agreeing (which they do right after a
    fresh `pip install -e .`) would make this pass without proving the
    ordering at all. The expected value is read from pyproject.toml too, so
    a version bump does not break this test - it used to hard-code the
    number and failed on the 0.1.2b1 -> 0.1.3b1 bump."""
    import re
    from pathlib import Path

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    expected = re.search(
        r'^\s*version\s*=\s*"([^"]+)"', pyproject.read_text(encoding="utf-8"), re.MULTILINE
    ).group(1)

    monkeypatch.setattr(
        "importlib.metadata.version", lambda name: "9.9.9-from-installed-metadata"
    )

    assert version_mod.app_version() == expected


def test_display_version_keeps_the_patch_component():
    """Used to stop at "major.minor" on the assumption the patch number never
    moved off 0 - once it did (0.1.0b1 -> 0.1.1b1), that dropped the one
    visible sign the version had even changed."""
    assert version_mod.display_version("0.1.1b1") == "0.1.1"
    assert version_mod.display_version("0.1.0b1") == "0.1.0"
    assert version_mod.display_version("1.2.3") == "1.2.3"


def test_display_version_does_not_leak_the_prerelease_digit():
    """_VERSION_PART.findall sees "0.1.1b1" as four numbers (0, 1, 1, 1) -
    the pre-release suffix is stripped by matching it explicitly, not by
    assuming which position its digit lands in."""
    assert version_mod.display_version("0.9.9b12") == "0.9.9"


def test_display_version_keeps_a_real_fourth_component():
    """A genuine patch release on top of a 3-part version ("0.1.8.1") is not
    the same shape as a pre-release digit landing in the same position - a
    fixed "keep the first 3 parts" cut used to drop this silently."""
    assert version_mod.display_version("0.1.8.1") == "0.1.8.1"


def test_display_version_strips_a_prerelease_suffix_even_with_a_real_patch_part():
    """The suffix and a real 4th component can coexist - only the matched
    suffix goes, the patch digit in front of it stays."""
    assert version_mod.display_version("0.1.8.1b1") == "0.1.8.1"


def test_the_shipped_version_is_a_beta():
    """Guards the pair: if pyproject is bumped to a final version this fails,
    which is the reminder to also decide what happens to the badge."""
    assert version_mod.release_label() == "b2"


def test_build_channel_reads_an_optional_pyproject_field(monkeypatch):
    monkeypatch.setattr(
        version_mod, "_pyproject_text",
        lambda: 'version = "0.1.1b1"\n[tool.bridgebox]\nchannel = "public"\n',
    )
    assert version_mod.build_channel() == "public"


def test_build_channel_is_empty_by_default():
    """This checkout's own pyproject.toml carries no `channel` line - only
    tools/build_portable.py's public-repo copy adds one, and only when
    building from a clone whose git remote is the real public repo."""
    assert version_mod.build_channel() == ""

