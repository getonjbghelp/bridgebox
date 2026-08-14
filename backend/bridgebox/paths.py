from __future__ import annotations

import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    # tools/build_portable.py's PyInstaller --onefile build. The one thing a
    # portable install must never do is write outside its own folder (no
    # %APPDATA%, no registry) - wherever this .exe was copied to IS the
    # install, so config.yaml/logs/certs/temp/zapret all live beside it.
    PROJECT_ROOT = Path(sys.executable).resolve().parent
    # Bundled read-only resources (frontend/dist, backend/pyproject.toml) live
    # inside the exe and are unpacked to sys._MEIPASS at startup - a real,
    # existing directory on disk, just not this one. Nothing the app WRITES
    # ever uses this; only the frontend build and version.py's frozen path
    # read from it. See tools/build_portable.py for what gets bundled where.
    RESOURCE_ROOT = Path(sys._MEIPASS)  # type: ignore[attr-defined]
else:
    # backend/bridgebox/paths.py -> parents[2] is the repo root (sibling of
    # frontend/ and zapret/) - same convention desktop.py's FRONTEND_DIST used.
    PROJECT_ROOT = Path(__file__).resolve().parents[2]
    RESOURCE_ROOT = PROJECT_ROOT


def resolve_project_path(project_root: Path, value: str) -> Path:
    """Resolve a config path value against project_root, unless it's already
    absolute. Relative config defaults (e.g. "zapret", "certs/") only make
    sense relative to the project root, not whatever the process's current
    working directory happens to be at launch time."""
    path = Path(value)
    return path if path.is_absolute() else project_root / path
