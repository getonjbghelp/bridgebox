"""Adapt a raw Flowseal general*.bat into a BridgeBox strategy.

This is a token whitelist generator, not a text transformer. A .bat file that
runs as Administrator is a code-execution sink, so the downloaded text is
never written to disk or executed as-is: it is parsed into --flag/value
tokens, only the tokens that survive a strict allowlist are kept, and the
result is rendered into BridgeBox's own fixed template. Same reasoning as
update.py's extraction boundary, one step further down the chain - see
zapret/README.md for why the originals cannot simply be copied in.

Flowseal's general*.bat files all share one shape: nine `--new`-separated
winws.exe profiles (six for the ALT5-style aggressive variants), targeting
different traffic (Discord voice, discord.media, Google-only, the general
web, game ports, ...). Empirically (verified against all 21 currently
shipped adaptations, not just a handful) exactly two of those profiles are
ever relevant to Jackbox traffic:

- the TCP profile whose --filter-tcp covers both port 80 and 443 (this is
  what excludes the discord.media-only and Google-only profiles) and prefers
  --hostlist targeting over --ipset when both exist for the same behaviour
- the UDP/QUIC profile carrying --dpi-desync-fake-quic, expressed either as
  --filter-udp=443 or (older Flowseal releases, e.g. general (EXP).bat) as
  --filter-l7=quic, again preferring --hostlist over --ipset

Everything else about a profile - its --filter-tcp/udp/l7, --hostlist(s),
--ipset(s), --hostlist-exclude, --ip-id, --dpi-desync-any-protocol, --dpi-
desync-cutoff - is BridgeBox's own targeting, not the original's, so it is
discarded. Only flags starting with "dpi-desync" are kept, in their original
relative order, values included.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# A profile is a list of (flag, value) pairs in original order; value is ""
# for a bare flag (e.g. --dpi-desync-any-protocol has no "=value" form here,
# though none of the kept dpi-desync flags happen to be bare in practice).
Profile = list[tuple[str, str]]

_NEW_SPLIT_RE = re.compile(r'\s+--new\s+')
_TOKEN_RE = re.compile(r'"[^"]*"|\S+')

# Everything a kept flag's VALUE is allowed to contain, once %BIN%<name> is
# handled separately below. No quotes, no &|<>^ shell metacharacters, no
# newlines - a value is rendered straight into our own double-quoted
# template string, so anything that could close that quote or start a new
# batch command is refused rather than sanitised.
_SAFE_VALUE_RE = re.compile(r'^[A-Za-z0-9_.,:=!^+-]*$')
_BIN_VALUE_RE = re.compile(r'^%BIN%([A-Za-z0-9_.-]+\.bin)$')

_TCP_PORTS_REQUIRED = {"80", "443"}

# The one --dpi-desync primitive with nothing to fool: it writes fake data
# straight into the real SYN, the packet every server on the path actually
# parses, unlike fake/multisplit/multidisorder which forge a throwaway
# packet a --dpi-desync-fooling flag makes the real server discard. Shared
# with strategies.py's Strategy.aggressive (computed from the rendered file,
# after this module has already run) so both sides of "is this strategy
# aggressive" agree on the one token that answers it.
#
# Confirmed against a real regression, not theory: Alternative 5's
# `syndata,multidisorder` with no --dpi-desync-fooling at all was the one
# shipped strategy that corrupted Blobcast's long-lived WebSocket ~20-30s in.
AGGRESSIVE_DESYNC_MODE = "syndata"


def _is_aggressive_profile(profile: Profile) -> bool:
    return any(
        flag == "dpi-desync" and AGGRESSIVE_DESYNC_MODE in value.split(",")
        for flag, value in profile
    )

# Blobcast's socket.io session (Party Pack 1-6) runs here, established by
# packet capture. It has to be in BOTH filters or it gets no DPI bypass at
# all: --wf-tcp decides what WinDivert captures, --filter-tcp which profile
# handles it. Measured symptom of it being absent: the first connection to
# the port succeeds and every repeat times out, which is what made the long
# socket.io session unstable. Kept in sync with server/blobcast.py's
# SOCKETIO_PORT by test_blobcast_port_is_in_the_dpi_filters.
BLOBCAST_PORT = 38203


@dataclass(frozen=True)
class AdaptResult:
    ok: bool
    filename: str
    content: str | None = None
    reason: str | None = None


def _parse_profile(raw: str) -> Profile:
    """Tokenize one --new-delimited profile into (flag, value) pairs, kept
    only if the flag name and value both pass the safety allowlist."""
    pairs: Profile = []
    for token in _TOKEN_RE.findall(raw):
        if not token.startswith("--"):
            continue
        name, _, value = token[2:].partition("=")
        value = value.strip('"')
        if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
            continue
        if value and not (_SAFE_VALUE_RE.match(value) or _BIN_VALUE_RE.match(value)):
            continue
        pairs.append((name, value))
    return pairs


def parse_profiles(winws_args: str) -> list[Profile]:
    """Split a winws.exe argument string (line continuations already joined
    or not - both handled) into per-profile (flag, value) lists."""
    joined = re.sub(r"\^\s*\r?\n\s*", " ", winws_args)
    return [_parse_profile(chunk) for chunk in _NEW_SPLIT_RE.split(joined) if chunk.strip()]


def _flag(profile: Profile, name: str) -> str | None:
    for flag, value in profile:
        if flag == name:
            return value
    return None


def _has_flag(profile: Profile, name: str) -> bool:
    return any(flag == name for flag, _ in profile)


def _tcp_ports(profile: Profile) -> set[str]:
    value = _flag(profile, "filter-tcp")
    return set(value.split(",")) if value else set()


def select_tcp_profile(profiles: list[Profile]) -> Profile | None:
    """The general-web TCP profile: --filter-tcp covering both 80 and 443.

    Discord-voice and discord.media-only profiles never carry both ports
    (they use --filter-l7=discord,stun or a bare 2053/2083/... range), so
    they are excluded by the port check alone; --filter-l7-scoped profiles
    are excluded outright since our own --filter-tcp is what does the
    scoping. Among the ones that qualify, --hostlist wins over --ipset."""
    candidates = [
        p for p in profiles
        if _TCP_PORTS_REQUIRED <= _tcp_ports(p) and not _has_flag(p, "filter-l7")
    ]
    if not candidates:
        return None
    for p in candidates:
        if not _has_flag(p, "ipset"):
            return p
    return candidates[0]


def select_udp_profile(profiles: list[Profile]) -> Profile | None:
    """The QUIC profile: carries --dpi-desync-fake-quic and is scoped to
    port 443 - either directly (--filter-udp=443) or, in older releases,
    via --filter-l7=quic. --hostlist wins over --ipset."""
    candidates = [
        p for p in profiles
        if _has_flag(p, "dpi-desync-fake-quic")
        and (_flag(p, "filter-udp") == "443" or _flag(p, "filter-l7") == "quic")
    ]
    if not candidates:
        return None
    for p in candidates:
        if not _has_flag(p, "ipset"):
            return p
    return candidates[0]


def missing_bin_refs(profile: Profile, available: set[str]) -> set[str]:
    """%BIN%<name> references a profile makes that are not among the files
    this install (or this staged update) actually has."""
    missing = set()
    for _, value in profile:
        m = _BIN_VALUE_RE.match(value)
        if m and m.group(1) not in available:
            missing.add(m.group(1))
    return missing


def _dpi_desync_flags(profile: Profile) -> Profile:
    return [(f, v) for f, v in profile if f.startswith("dpi-desync")]


def _render_profile_lines(profile: Profile) -> list[str]:
    lines = []
    for flag, value in _dpi_desync_flags(profile):
        if value and not (_SAFE_VALUE_RE.match(value) or _BIN_VALUE_RE.match(value)):
            continue  # belt and braces; _parse_profile already filtered this
        if value.startswith("%BIN%"):
            lines.append(f'--{flag}="{value}"')
        elif value:
            lines.append(f"--{flag}={value}")
        else:
            lines.append(f"--{flag}")
    return lines


def render_bat(original_name: str, tcp_profile: Profile, udp_profile: Profile) -> str:
    tcp_lines = _render_profile_lines(tcp_profile)
    udp_lines = _render_profile_lines(udp_profile)
    # Computed fresh on every regeneration, not hand-written: a hand-added
    # warning comment here was erased by the very next `--update-zapret` run,
    # since this function rewrites the whole header unconditionally. Deriving
    # it from the same profile the rest of this function already has means
    # there is nothing left for a regeneration to forget.
    aggressive = _is_aggressive_profile(tcp_profile) or _is_aggressive_profile(udp_profile)

    body_lines = [
        f"  --wf-tcp=80,443,{BLOBCAST_PORT} ^",
        "  --wf-udp=443 ^",
        f"  --filter-tcp=80,443,{BLOBCAST_PORT} ^",
        '  --hostlist="%HOSTLIST%" ^',
        *(f"  {line} ^" for line in tcp_lines),
        "  --new ^",
        "  --filter-udp=443 ^",
        '  --hostlist="%HOSTLIST%" ^',
        *(f"  {line} ^" for line in udp_lines[:-1]),
        f"  {udp_lines[-1]}" if udp_lines else '  --dpi-desync=fake',
    ]

    return (
        "@echo off\n"
        "chcp 65001 >nul\n"
        f':: BridgeBox-adapted Flowseal "{original_name}".\n'
        + (
            ":: NOT RECOMMENDED for long-lived connections (e.g. Blobcast) - this\n"
            ":: strategy's desync writes into the real SYN with no --dpi-desync-\n"
            ":: fooling flag to shield a live server from it. Use only if gentler\n"
            ":: strategies fail; see the Settings/Setup Wizard warning for why.\n"
            if aggressive
            else ""
        )
        + ":: Auto-generated by bridgebox.zapret.strategy_adapt - selected the TCP\n"
        ":: 80+443 and QUIC profiles from the original, discarded the rest.\n"
        ":: Layout: winws.exe + WinDivert + *.bin live in vendor/zapret/ (parent of this file).\n"
        ':: Runs winws in the foreground (no "start") so BridgeBox can track the process.\n'
        ":: Domains come only from lists\\list-jackbox.txt (--hostlist).\n"
        "\n"
        "setlocal\n"
        'cd /d "%~dp0.."\n'
        'set "BIN=%cd%\\"\n'
        'set "LISTS=%cd%\\lists\\"\n'
        'set "HOSTLIST=%LISTS%list-jackbox.txt"\n'
        "\n"
        'if exist "%BIN%winws.exe" goto :bb_check_hostlist\n'
        'echo [BridgeBox] winws.exe not found in "%BIN%"\n'
        "exit /b 1\n"
        "\n"
        ":bb_check_hostlist\n"
        'if exist "%HOSTLIST%" goto :bb_run\n'
        'echo [BridgeBox] hostlist not found: "%HOSTLIST%"\n'
        "exit /b 1\n"
        "\n"
        ":bb_run\n"
        "\n"
        '"%BIN%winws.exe" ^\n'
        + "\n".join(body_lines) + "\n"
        "\n"
        "endlocal\n"
    )


# What a generated strategy filename may contain. The name originates in a
# DOWNLOADED archive's member list, and the file it names would be executed as
# Administrator, so it is built from an allowlist for the same reason the flag
# values are. ":" is the one that matters most on Windows: "general:x.bat" is
# not a filename at all, it is an alternate data stream on "general".
_SAFE_STEM_RE = re.compile(r"^[A-Za-z0-9 ()._-]+$")


_QUALIFIER_RE = re.compile(r"^General \((.+)\)$")


def _pretty_qualifier(qualifier: str) -> str | None:
    """Maps a Flowseal general*.bat qualifier onto the hand-picked name
    BridgeBox's own shipped strategies already use for it (ALT13 -> Alternative
    13, FAKE TLS AUTO ALT2 -> Fake TLS Auto 2, ...) - confirmed against all 21
    currently shipped adaptations plus ALT13's real-world debut, every
    qualifier in these three families maps onto an existing name with no
    ambiguity. This used to be left as the raw "General (ALT13)" on the theory
    that guessing a name was riskier than not - but plan_strategies matches
    strategies by filename, so NOT mapping it was the real risk: every release
    silently added a byte-for-byte duplicate of an existing strategy under
    the wrong name instead of recognizing and updating it.

    A qualifier outside these three families (something Flowseal adds later
    that nobody has mapped yet) returns None, and the caller keeps the raw
    "General (<qualifier>)" name rather than guessing at it."""
    m = re.fullmatch(r"ALT(\d*)", qualifier)
    if m:
        return f"Alternative {m.group(1) or 1}"
    m = re.fullmatch(r"SIMPLE FAKE(?: ALT(\d*))?", qualifier)
    if m:
        variant = m.group(1)
        return "Simple Fake" if variant is None else f"Simple Fake {variant or 1}"
    m = re.fullmatch(r"FAKE TLS AUTO(?: ALT(\d*))?", qualifier)
    if m:
        variant = m.group(1)
        return "Fake TLS Auto" if variant is None else f"Fake TLS Auto {variant or 1}"
    return None


def suggest_filename(original_name: str) -> str:
    """general (ALT13).bat -> Alternative 13.bat when the qualifier matches a
    known family (see _pretty_qualifier); general (EXP).bat -> General
    (EXP).bat, and anything else unrecognized, unchanged - EXP has no
    established pretty name of its own to map to.

    Raises ValueError on a stem that is not a plain filename - see
    _SAFE_STEM_RE. Callers treat that as "cannot adapt this one"."""
    stem = original_name[:-4] if original_name.lower().endswith(".bat") else original_name
    if stem.lower().startswith("general"):
        stem = "General" + stem[len("general"):]
    match = _QUALIFIER_RE.match(stem)
    if match:
        pretty = _pretty_qualifier(match.group(1))
        if pretty is not None:
            stem = pretty
    if not _SAFE_STEM_RE.match(stem) or stem.strip(" .") != stem:
        raise ValueError(f"unsafe strategy filename: {original_name!r}")
    return f"{stem}.bat"


# Provenance, so an update can tell its own previous output apart from a file
# the user has edited. Appended as the LAST line, after `endlocal`, and hashing
# only the text above it - which keeps `_WINWS_ARGS_RE` in the golden test and
# every test_strategy_assets invariant looking at exactly what they looked at
# before, and avoids any self-referential-hash awkwardness.
STAMP_PREFIX = ":: BridgeBox-stamp "


def stamp(content: str, version: str) -> str:
    """Append the provenance line to a rendered strategy."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"{content}{STAMP_PREFIX}{version} {digest}\n"


def is_unmodified_generated(text: str) -> bool:
    """True only for a file BridgeBox generated and nobody has touched since.

    Deliberately conservative: anything that fails - no stamp, a malformed
    stamp, a hash that no longer matches, even a text editor having rewritten
    the line endings - reads as "the user's file". The consequence of a false
    negative is one extra file on disk; the consequence of a false positive is
    silently destroying someone's hand-tuned strategy.

    ponytail: byte-exact, so a CRLF round trip through Notepad forfeits the
    stamp. Normalising line endings before hashing would fix that and is the
    upgrade path if it ever bites - but it also widens what counts as
    "unmodified", so it needs a reason beyond tidiness.
    """
    body, separator, last = text.rpartition(f"\n{STAMP_PREFIX}")
    if not separator:
        return False
    parts = last.strip().split()
    if len(parts) != 2:
        return False
    return hashlib.sha256(f"{body}\n".encode("utf-8")).hexdigest() == parts[1]


def adapt_strategy(
    original_name: str, original_text: str, *, available_bins: set[str] | None = None
) -> AdaptResult:
    try:
        filename = suggest_filename(original_name)
    except ValueError as exc:
        return AdaptResult(False, original_name, reason=str(exc))
    profiles = parse_profiles(original_text)

    tcp_profile = select_tcp_profile(profiles)
    if tcp_profile is None:
        return AdaptResult(False, filename, reason="no TCP 80+443 profile found")

    udp_profile = select_udp_profile(profiles)
    if udp_profile is None:
        return AdaptResult(False, filename, reason="no QUIC profile found")

    if available_bins is not None:
        missing = missing_bin_refs(tcp_profile, available_bins) | missing_bin_refs(
            udp_profile, available_bins
        )
        if missing:
            return AdaptResult(
                False, filename, reason=f"references missing payload(s): {sorted(missing)}"
            )

    return AdaptResult(True, filename, content=render_bat(original_name, tcp_profile, udp_profile))
