from pathlib import Path

import pytest

from bridgebox.zapret import strategies as strategies_module
from bridgebox.zapret.strategies import (
    discover_strategies,
    group_strategies,
    resolve_strategy,
    resolve_zapret_layout,
)


def _make_bats(strategies_dir: Path, names: list[str]) -> None:
    strategies_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        (strategies_dir / name).write_text("@echo off\n")
    (strategies_dir / ".gitkeep").write_text("")


REAL_LAYOUT = [
    "General.bat",
    "Alternative 1.bat",
    "Alternative 2.bat",
    "Alternative 10.bat",
    "Alternative 11.bat",
    "Alternative 12.bat",
    "Fake TLS Auto.bat",
    "Fake TLS Auto 1.bat",
    "Simple Fake.bat",
    "Simple Fake 1.bat",
    "general (EXP) - UNOPTIMISED.bat",
]


def test_discover_strategies_finds_bat_files_and_slugs_keys(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    _make_bats(strategies_dir, REAL_LAYOUT)

    strategies = discover_strategies(strategies_dir)

    assert "general" in strategies
    assert strategies["general"].filename == "General.bat"
    assert "alternative-11" in strategies
    assert strategies["alternative-11"].filename == "Alternative 11.bat"
    assert "fake-tls-auto" in strategies
    assert "simple-fake-1" in strategies


def test_discover_strategies_ignores_non_bat_files(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    _make_bats(strategies_dir, REAL_LAYOUT)

    strategies = discover_strategies(strategies_dir)

    assert all(s.filename != ".gitkeep" for s in strategies.values())
    assert len(strategies) == len(REAL_LAYOUT)


def test_group_general_is_primary(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    _make_bats(strategies_dir, REAL_LAYOUT)

    strategies = discover_strategies(strategies_dir)

    assert strategies["general"].group == "Основная"


def test_group_alternatives(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    _make_bats(strategies_dir, REAL_LAYOUT)

    strategies = discover_strategies(strategies_dir)

    assert strategies["alternative-1"].group == "Альтернативы"
    assert strategies["alternative-12"].group == "Альтернативы"


def test_group_everything_else_is_other(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    _make_bats(strategies_dir, REAL_LAYOUT)

    strategies = discover_strategies(strategies_dir)

    assert strategies["fake-tls-auto"].group == "Прочие"
    assert strategies["simple-fake"].group == "Прочие"
    assert strategies["general-exp-unoptimised"].group == "Прочие"


def test_group_strategies_natural_sort_order_within_alternatives(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    _make_bats(strategies_dir, REAL_LAYOUT)

    strategies = discover_strategies(strategies_dir)
    grouped = group_strategies(strategies)

    alt_filenames = [s.filename for s in grouped["Альтернативы"]]
    assert alt_filenames == [
        "Alternative 1.bat",
        "Alternative 2.bat",
        "Alternative 10.bat",
        "Alternative 11.bat",
        "Alternative 12.bat",
    ]


def test_resolve_strategy_direct_key(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    _make_bats(strategies_dir, REAL_LAYOUT)
    strategies = discover_strategies(strategies_dir)

    resolved = resolve_strategy("general", strategies)

    assert resolved.filename == "General.bat"


def test_resolve_legacy_alias_when_target_exists(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    _make_bats(strategies_dir, REAL_LAYOUT)  # includes "Alternative 11.bat"
    strategies = discover_strategies(strategies_dir)

    resolved = resolve_strategy("general-alt11", strategies)

    assert resolved.filename == "Alternative 11.bat"


def test_resolve_legacy_alias_raises_when_target_missing(tmp_path: Path, monkeypatch):
    """An alias whose .bat was never shipped must fail naming the target, not
    the alias - otherwise the error reads as "unknown strategy" and sends you
    looking in config.yaml instead of at the alias table. The alias is injected
    rather than taken from LEGACY_ALIASES so this keeps covering the branch no
    matter which aliases ship (two dead ones were removed once already)."""
    monkeypatch.setitem(strategies_module.LEGACY_ALIASES, "ghost", "Never Shipped.bat")

    strategies_dir = tmp_path / "strategies"
    _make_bats(strategies_dir, REAL_LAYOUT)  # no "Never Shipped.bat"
    strategies = discover_strategies(strategies_dir)

    with pytest.raises(KeyError, match="Never Shipped.bat"):
        resolve_strategy("ghost", strategies)


def test_resolve_unknown_key_raises(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    _make_bats(strategies_dir, REAL_LAYOUT)
    strategies = discover_strategies(strategies_dir)

    with pytest.raises(KeyError):
        resolve_strategy("does-not-exist", strategies)


def test_resolve_zapret_layout_relative_dir(tmp_path: Path):
    layout = resolve_zapret_layout(tmp_path / "zapret")

    assert layout.strategies_dir == tmp_path / "zapret" / "strategies"
    assert layout.hostlist_path == tmp_path / "zapret" / "lists" / "list-jackbox.txt"


def test_resolve_zapret_layout_accepts_string_path(tmp_path: Path):
    layout = resolve_zapret_layout(str(tmp_path / "zapret"))

    assert layout.strategies_dir == tmp_path / "zapret" / "strategies"


def test_validate_hostlist_keeps_comments_and_blanks(tmp_path: Path):
    hosts = strategies_module.validate_hostlist(
        "# Ecast API\n\necast.jackboxgames.com\n  jackbox.tv  \n"
    )

    assert hosts == ["ecast.jackboxgames.com", "jackbox.tv"]


@pytest.mark.parametrize(
    "bad",
    [
        "https://ecast.jackboxgames.com",  # a URL, not a hostname
        "ecast.jackboxgames.com:443",  # host:port
        "ecast.jackboxgames.com extra",  # two names on one line
        "a" * 254,  # over the RFC 1035 ceiling
        "# only comments\n\n",  # nothing for winws to match
    ],
)
def test_validate_hostlist_rejects_what_winws_would_silently_ignore(bad: str):
    with pytest.raises(ValueError):
        strategies_module.validate_hostlist(bad)


def test_save_hostlist_writes_atomically_and_leaves_no_temp(tmp_path: Path):
    path = tmp_path / "lists" / "list-jackbox.txt"
    path.parent.mkdir(parents=True)
    path.write_text("old.example.com\n", encoding="utf-8")

    hosts = strategies_module.save_hostlist(path, "# new\necast.jackboxgames.com")

    assert hosts == ["ecast.jackboxgames.com"]
    # Trailing newline added, so the last host isn't glued to whatever a later
    # append writes.
    assert path.read_text(encoding="utf-8") == "# new\necast.jackboxgames.com\n"
    assert list(path.parent.iterdir()) == [path]


def test_save_hostlist_leaves_the_previous_file_intact_when_invalid(tmp_path: Path):
    path = tmp_path / "list-jackbox.txt"
    path.write_text("ecast.jackboxgames.com\n", encoding="utf-8")

    with pytest.raises(ValueError):
        strategies_module.save_hostlist(path, "https://nope")

    assert path.read_text(encoding="utf-8") == "ecast.jackboxgames.com\n"
