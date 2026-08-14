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


def test_pyproject_wins_over_installed_metadata():
    """Measured, not assumed: `pip install -e .` records the version at install
    time, so an editable checkout kept reporting "0.1.0" while pyproject
    already said "0.1.0b1". The file the developer edits has to win."""
    assert version_mod.app_version() == "0.1.0b1"


def test_the_shipped_version_is_a_beta():
    """Guards the pair: if pyproject is bumped to a final version this fails,
    which is the reminder to also decide what happens to the badge."""
    assert version_mod.release_label() == "b1"
