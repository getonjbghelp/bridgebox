from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

GroupName = Literal["Основная", "Альтернативы", "Прочие"]

# Old config keys some early BridgeBox builds shipped with, kept resolvable
# so existing user configs don't break (see zapret/README.md "Legacy names").
# An alias whose target isn't in strategies/ is worse than no alias: it turns
# an "unknown strategy" error into one naming a .bat that was never shipped.
# Only add entries whose target actually exists - test_strategy_assets.py's
# sibling invariants are the pattern to follow if this list grows.
LEGACY_ALIASES: dict[str, str] = {
    "general-alt11": "Alternative 11.bat",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_NUMBER_RE = re.compile(r"(\d+)")


def _slug(stem: str) -> str:
    return _SLUG_RE.sub("-", stem.lower()).strip("-")


def _group_for(stem: str) -> GroupName:
    lowered = stem.lower()
    if lowered == "general":
        return "Основная"
    if lowered.startswith("alternative"):
        return "Альтернативы"
    return "Прочие"


def _natural_sort_key(stem: str) -> tuple:
    parts = _NUMBER_RE.split(stem)
    return tuple(int(part) if part.isdigit() else part.lower() for part in parts)


@dataclass(frozen=True)
class Strategy:
    key: str
    filename: str
    path: Path
    group: GroupName


@dataclass(frozen=True)
class ZapretLayout:
    strategies_dir: Path
    hostlist_path: Path


def resolve_zapret_layout(zapret_dir: str | Path) -> ZapretLayout:
    """Resolve the fixed on-disk layout under a zapret root dir (winws.exe,
    strategies/, lists/list-jackbox.txt) - kept out of config.py so the
    config schema stays pure data with no path-joining logic."""
    zapret_dir = Path(zapret_dir)
    return ZapretLayout(
        strategies_dir=zapret_dir / "strategies",
        hostlist_path=zapret_dir / "lists" / "list-jackbox.txt",
    )


# RFC 1035's wire-format ceiling for a domain name. winws.exe matches these
# against the SNI, so anything longer could never match a real handshake.
MAX_HOSTNAME_LEN = 253


def validate_hostlist(text: str) -> list[str]:
    """Return the hostnames in `text`, raising ValueError on anything winws
    wouldn't accept. Blank lines and `#` comments are ignored, exactly as
    winws treats them, so the file stays commentable.

    Validated here, at the point the user types it, rather than left to
    winws: winws doesn't reject a malformed line, it silently ignores it, and
    the symptom only appears as "zapret didn't bypass anything" the next time
    the bridge starts - arbitrarily far from the edit that caused it."""
    hosts: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if any(char.isspace() for char in line) or "/" in line or ":" in line:
            raise ValueError(
                f"строка {number}: нужно только имя хоста, без пробелов, «/» и «:» — {line!r}"
            )
        if len(line) > MAX_HOSTNAME_LEN:
            raise ValueError(f"строка {number}: имя хоста длиннее {MAX_HOSTNAME_LEN} символов")
        hosts.append(line)

    if not hosts:
        raise ValueError("список пуст — без единого хоста zapret не знает, что обходить")
    return hosts


def save_hostlist(path: str | Path, text: str) -> list[str]:
    """Validate and write the hostlist, returning the hostnames written.

    Writes to a temp file and renames: os.replace is atomic within a volume,
    so a crash mid-write leaves the previous list intact instead of a
    truncated one - and this file is read by winws.exe at every bridge
    start."""
    hosts = validate_hostlist(text)
    path = Path(path)
    if not text.endswith("\n"):
        text += "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    logger.info("hostlist saved: %d hosts -> %s", len(hosts), path)
    return hosts


def discover_strategies(strategies_dir: str | Path) -> dict[str, Strategy]:
    """Scan strategies_dir for *.bat files and build a key -> Strategy map.
    Non-.bat infra files (.gitkeep etc.) are excluded by the glob itself."""
    strategies_dir = Path(strategies_dir)
    if not strategies_dir.is_dir():
        # An empty strategy dropdown with no explanation is one of the least
        # debuggable states the Settings screen can reach.
        logger.warning("strategies dir does not exist: %s", strategies_dir)
        return {}

    bat_paths = sorted(strategies_dir.glob("*.bat"), key=lambda p: _natural_sort_key(p.stem))

    strategies: dict[str, Strategy] = {}
    for bat_path in bat_paths:
        key = _slug(bat_path.stem)
        if key in strategies:
            logger.warning(
                "strategy key collision %r: %s overrides %s",
                key,
                bat_path.name,
                strategies[key].filename,
            )
        strategies[key] = Strategy(
            key=key,
            filename=bat_path.name,
            path=bat_path,
            group=_group_for(bat_path.stem),
        )
        logger.debug("discovered strategy %r -> %s [%s]", key, bat_path.name, _group_for(bat_path.stem))

    logger.info("discovered %d strategies in %s", len(strategies), strategies_dir)
    if not strategies:
        logger.warning("no *.bat strategies found in %s", strategies_dir)
    return strategies


def group_strategies(strategies: dict[str, Strategy]) -> dict[GroupName, list[Strategy]]:
    grouped: dict[GroupName, list[Strategy]] = {
        "Основная": [],
        "Альтернативы": [],
        "Прочие": [],
    }
    # discover_strategies already yields entries in natural-sort order, and
    # dict insertion order is preserved, so each group list stays sorted too.
    for strategy in strategies.values():
        grouped[strategy.group].append(strategy)
    return grouped


def resolve_strategy(key: str, strategies: dict[str, Strategy]) -> Strategy:
    """Resolve a config strategy key to a Strategy, honoring legacy aliases."""
    if key in strategies:
        logger.debug("resolved strategy %r -> %s", key, strategies[key].filename)
        return strategies[key]

    target_filename = LEGACY_ALIASES.get(key)
    if target_filename is not None:
        for strategy in strategies.values():
            if strategy.filename == target_filename:
                logger.info(
                    "config uses legacy strategy alias %r -> %s", key, strategy.filename
                )
                return strategy
        logger.error(
            "legacy alias %r points at %r, which is missing from strategies/ "
            "(available: %s)",
            key,
            target_filename,
            ", ".join(sorted(strategies)) or "<none>",
        )
        raise KeyError(
            f"legacy strategy alias '{key}' points at '{target_filename}', "
            "which is not present in strategies/"
        )

    logger.error(
        "unknown strategy %r in config (available: %s)",
        key,
        ", ".join(sorted(strategies)) or "<none>",
    )
    raise KeyError(f"unknown zapret strategy '{key}'")
