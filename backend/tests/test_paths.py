from pathlib import Path

from bridgebox.paths import PROJECT_ROOT, resolve_project_path


def test_project_root_is_the_repo_root():
    # backend/bridgebox/paths.py -> parents[2] is the repo root, sibling of
    # frontend/ and zapret/ (matches desktop.py's FRONTEND_DIST convention).
    assert (PROJECT_ROOT / "frontend").is_dir()
    assert (PROJECT_ROOT / "zapret").is_dir()
    assert (PROJECT_ROOT / "backend").is_dir()


def test_resolve_project_path_joins_relative_value(tmp_path: Path):
    result = resolve_project_path(tmp_path, "zapret")
    assert result == tmp_path / "zapret"


def test_resolve_project_path_keeps_absolute_value_as_is(tmp_path: Path):
    absolute = tmp_path / "elsewhere" / "zapret"
    result = resolve_project_path(tmp_path / "unrelated-root", str(absolute))
    assert result == absolute
