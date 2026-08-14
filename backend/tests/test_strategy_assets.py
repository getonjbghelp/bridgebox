"""Asset invariants for the adapted zapret strategies.

Both directions matter, and both have been violated in this repo:

- A .bat referencing a file that isn't there fails at *bridge start*, inside
  winws.exe, with a message far from the cause.
- A payload file no .bat references is a silent regression: three strategies
  had quietly substituted tls_clienthello_max_ru.bin for the pattern the
  Flowseal original actually used, leaving stun2.bin and
  tls_clienthello_4pda_to.bin dead on disk.

originalstrategies/ is deliberately excluded - those are unmodified Flowseal
files kept for reference, and they reference a bin/ + lists/ layout that was
never vendored here (see zapret/README.md).
"""

import re
from pathlib import Path

from bridgebox.paths import PROJECT_ROOT
from bridgebox.zapret.strategies import LEGACY_ALIASES

ZAPRET_DIR = PROJECT_ROOT / "zapret"
STRATEGIES_DIR = ZAPRET_DIR / "strategies"

# "%BIN%name.bin" -> name.bin. The .bat files set BIN to the zapret root.
_BIN_REF = re.compile(r"%BIN%([^\"^\s]+)")


def _adapted_bats() -> list[Path]:
    # glob("*.bat") is already non-recursive, so originalstrategies/ is out.
    return sorted(STRATEGIES_DIR.glob("*.bat"))


def _referenced_assets() -> dict[str, set[str]]:
    """asset filename -> set of .bat names referencing it."""
    refs: dict[str, set[str]] = {}
    for bat in _adapted_bats():
        for name in _BIN_REF.findall(bat.read_text(encoding="utf-8")):
            refs.setdefault(name, set()).add(bat.name)
    return refs


def test_there_are_strategies_to_check():
    # Guards the two tests below from passing vacuously if the glob breaks.
    assert len(_adapted_bats()) > 10


def test_every_referenced_asset_exists():
    missing = {
        name: sorted(bats)
        for name, bats in _referenced_assets().items()
        if not (ZAPRET_DIR / name).exists()
    }

    assert not missing, f"strategies reference files that are not in {ZAPRET_DIR}: {missing}"


def test_every_payload_file_is_used_by_some_strategy():
    referenced = set(_referenced_assets())
    on_disk = {path.name for path in ZAPRET_DIR.glob("*.bin")}

    assert on_disk, "no *.bin payloads found - wrong directory?"
    unused = sorted(on_disk - referenced)

    assert not unused, (
        f"payload files present but referenced by no strategy: {unused}. "
        "Either a strategy silently substituted a different pattern, or the file is dead."
    )


def test_every_legacy_alias_points_at_a_shipped_strategy():
    """Two aliases once pointed at .bat files that were never shipped, so a
    config carrying either one failed at bridge start naming a file nobody
    could find."""
    shipped = {path.name for path in _adapted_bats()}
    broken = {alias: target for alias, target in LEGACY_ALIASES.items() if target not in shipped}

    assert not broken, f"legacy aliases point at strategies that do not exist: {broken}"


def test_blobcast_port_is_in_the_dpi_filters():
    """Blobcast's socket.io session (Party Pack 1-6) runs on 38203, and it
    needs the port in BOTH filters: --wf-tcp decides what WinDivert captures
    at all, --filter-tcp which profile handles it. With it absent the port
    got no bypass whatsoever - measured symptom was the first connection
    succeeding and every repeat timing out, which is what made the long
    socket.io session unstable.

    Guards every strategy, including any the adapter generates later, and
    pins the number to the one the interceptor actually listens on."""
    from bridgebox.server.blobcast import SOCKETIO_PORT

    missing = {}
    for bat in _adapted_bats():
        text = bat.read_text(encoding="utf-8")
        gaps = [
            flag
            for flag in ("--wf-tcp=", "--filter-tcp=")
            if f"{flag}80,443,{SOCKETIO_PORT}" not in text
        ]
        if gaps:
            missing[bat.name] = gaps

    assert not missing, f"strategies missing port {SOCKETIO_PORT} in the DPI filters: {missing}"
