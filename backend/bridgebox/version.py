"""The app's own version, and the short label the UI shows for it.

One source: pyproject.toml's `version`, read through importlib.metadata when
BridgeBox is installed and parsed out of the file when it is not (running from
a checkout, which is how run.bat starts it). Duplicating the number into a
Python constant is how the two would drift.

PEP 440 is what pyproject must hold, so the first beta is "0.1.0b1" - a bare
"b1" is not a valid version and packaging tools reject it. The label the user
sees is derived from that, not stored separately: 0.1.0b1 -> "b1". When the
pre-release segment goes away, so does the label, and with it the Beta badge -
exactly what its tooltip promises.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# 1!2.3.4b5 -> the "b5" part. Only beta matters here; a/rc are matched too so
# the label stays honest if the project ever ships one.
_PRERELEASE_RE = re.compile(r"(?:a|b|rc)\d+$")
# The numeric components, in order. "0.1.0b1" -> ["0", "1", "0", "1"].
_VERSION_PART = re.compile(r"\d+")
_PYPROJECT_VERSION_RE = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.MULTILINE)

_FALLBACK = "0.0.0"


def _from_pyproject() -> str | None:
    """Walk up for backend/pyproject.toml. Running from a checkout - which is
    what run.bat does - means importlib.metadata has nothing to find."""
    if getattr(sys, "frozen", False):
        # __file__ inside a frozen module is not a real path on disk -
        # PyInstaller bundles .py sources into its own archive, so the
        # parents-walk below would never find anything to .exists() check.
        # tools/build_portable.py bundles pyproject.toml itself at the
        # sys._MEIPASS root specifically so this can still find it.
        candidate = Path(sys._MEIPASS) / "pyproject.toml"  # type: ignore[attr-defined]
        if not candidate.exists():
            return None
        match = _PYPROJECT_VERSION_RE.search(
            candidate.read_text(encoding="utf-8", errors="replace")
        )
        return match.group(1) if match else None

    for parent in Path(__file__).resolve().parents:
        candidate = parent / "pyproject.toml"
        if candidate.exists():
            match = _PYPROJECT_VERSION_RE.search(
                candidate.read_text(encoding="utf-8", errors="replace")
            )
            return match.group(1) if match else None
    return None


def app_version() -> str:
    """pyproject.toml first, installed metadata second.

    That order is deliberate and was measured: `pip install -e .` records the
    version at install time, so an editable checkout keeps reporting whatever
    it was installed as - this returned "0.1.0" while pyproject already said
    "0.1.0b1". The file the developer edits has to win.

    A frozen build has neither an editable install nor a loose pyproject.toml
    next to this file - see _from_pyproject's sys._MEIPASS branch, fed by
    tools/build_portable.py bundling pyproject.toml itself into the exe."""
    from_file = _from_pyproject()
    if from_file:
        return from_file

    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("bridgebox")
        except PackageNotFoundError:
            pass
    except ImportError:  # pragma: no cover - importlib.metadata is stdlib
        pass
    return _FALLBACK


def release_label(version_string: str | None = None) -> str:
    """Non-empty while this is a pre-release, empty once it is not.

    Only its emptiness is user-visible: the Beta badge renders only when this
    is non-empty, so shipping a final version removes the badge with no code
    change. The badge itself shows β, and the version shown beside it is
    display_version() - neither repeats this string."""
    match = _PRERELEASE_RE.search(version_string or app_version())
    return match.group(0) if match else ""


def display_version(version_string: str | None = None) -> str:
    """What the user is shown: "0.1".

    pyproject has to hold a PEP 440 version ("0.1.0b1") because packaging
    tools read it, but that string is noise to a player - the beta-ness is
    already said by the β badge, and the third component has never differed
    from 0. So the label is the first two components and nothing else."""
    parts = _VERSION_PART.findall(version_string or app_version())
    if not parts:
        return ""
    return ".".join(parts[:2]) if len(parts) >= 2 else parts[0]
