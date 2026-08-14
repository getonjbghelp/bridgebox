"""The one piece of tools/build_portable.py worth a regression test:
version_tuple()'s PEP 440 -> Windows PE FILEVERSION conversion. Everything
else in that script is either subprocess orchestration (PyInstaller, npm) or
filesystem assembly - exercised by actually running the pipeline, not by a
unit test standing in for one."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from build_portable import version_tuple  # noqa: E402


def test_a_beta_versions_prerelease_number_becomes_the_fourth_component():
    assert version_tuple("0.1.0b1") == (0, 1, 0, 1)


def test_two_betas_of_the_same_release_stay_distinguishable():
    assert version_tuple("0.1.0b1") != version_tuple("0.1.0b2")
    assert version_tuple("0.1.0b2") == (0, 1, 0, 2)


def test_a_final_release_has_no_prerelease_so_the_fourth_component_is_zero():
    assert version_tuple("1.2.3") == (1, 2, 3, 0)


def test_an_alpha_or_release_candidate_is_recognised_too():
    assert version_tuple("2.0.0a3") == (2, 0, 0, 3)
    assert version_tuple("2.0.0rc4") == (2, 0, 0, 4)


def test_a_two_component_version_is_padded_to_three():
    assert version_tuple("0.1") == (0, 1, 0, 0)
