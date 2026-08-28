"""The Phase 2 strategy adapter: turns a raw Flowseal general*.bat into a
BridgeBox .bat WITHOUT ever executing or copying the downloaded text. Only a
fixed set of tokens (--dpi-desync* flag names, and values matching a strict
charset or the %BIN%<safe-name>.bin pattern) survive extraction; everything
else - including the whole targeting/hostlist/ipset section - is discarded
and replaced with BridgeBox's own template. That is what keeps a .bat that
runs as Administrator from becoming a command-injection sink for whatever a
compromised or malicious release happens to ship (see zapret/README.md and
the Phase 2 section of the approved plan).

The golden test is the load-bearing one: the 21 Flowseal originals this repo
already ships adapted versions of are vendored as fixtures (recovered via
`git show e7eab7b^:zapret/strategies/originalstrategies/<name>`), and the
adapter must reproduce the SAME selected profiles - proving the selection
rule against the full known set, not just the 3 files a human spot-checked
when the rule was first written.

Profiles are compared as {flag_name: [values...]} rather than as raw text:
flag ORDER among distinct flag names has no effect on winws.exe (it parses
--name=value tokens, not a positional grammar), so a byte-exact diff would
fail on General (EXP).bat for no functional reason - that file's --dpi-
desync-repeats was hand-moved earlier by a human, and no rule should be
inferred from where one edit put it. Order among DUPLICATE flag names (e.g.
two --dpi-desync-fake-tls in a row) is preserved, since that IS meaningful.
"""
import re
from pathlib import Path

import pytest

from bridgebox.zapret import strategy_adapt as adapt

FIXTURES = Path(__file__).parent / "fixtures" / "flowseal-1.10" / "originalstrategies"
STRATEGIES_DIR = Path(__file__).parent.parent.parent / "zapret" / "strategies"

# original filename -> filename BridgeBox already ships an adapted version as.
KNOWN_ADAPTATIONS = {
    "general.bat": "General.bat",
    "general (ALT).bat": "Alternative 1.bat",
    "general (ALT2).bat": "Alternative 2.bat",
    "general (ALT3).bat": "Alternative 3.bat",
    "general (ALT4).bat": "Alternative 4.bat",
    "general (ALT5).bat": "Alternative 5.bat",
    "general (ALT6).bat": "Alternative 6.bat",
    "general (ALT7).bat": "Alternative 7.bat",
    "general (ALT8).bat": "Alternative 8.bat",
    "general (ALT9).bat": "Alternative 9.bat",
    "general (ALT10).bat": "Alternative 10.bat",
    "general (ALT11).bat": "Alternative 11.bat",
    "general (ALT12).bat": "Alternative 12.bat",
    "general (EXP).bat": "General (EXP).bat",
    "general (FAKE TLS AUTO).bat": "Fake TLS Auto.bat",
    "general (FAKE TLS AUTO ALT).bat": "Fake TLS Auto 1.bat",
    "general (FAKE TLS AUTO ALT2).bat": "Fake TLS Auto 2.bat",
    "general (FAKE TLS AUTO ALT3).bat": "Fake TLS Auto 3.bat",
    "general (SIMPLE FAKE).bat": "Simple Fake.bat",
    "general (SIMPLE FAKE ALT).bat": "Simple Fake 1.bat",
    "general (SIMPLE FAKE ALT2).bat": "Simple Fake 2.bat",
}

_WINWS_ARGS_RE = re.compile(r'"%BIN%winws\.exe"\s*\^?\s*\r?\n(.*?)\r?\n\r?\nendlocal', re.DOTALL)
_FLAG_TOKEN_RE = re.compile(r'--([a-zA-Z0-9][a-zA-Z0-9-]*)(?:="?([^"\s]*)"?)?')


def _profiles_as_flag_maps(winws_args_block: str) -> list[dict[str, list[str]]]:
    """Parse a rendered BridgeBox winws.exe argument block (whether generated
    or read off disk) into a list of {flag: [values]} dicts, one per --new
    separated profile, restricted to dpi-desync* flags for comparison."""
    joined = re.sub(r'\^\s*\r?\n\s*', ' ', winws_args_block)
    profiles = re.split(r'\s+--new\s+', joined)
    result = []
    for profile in profiles:
        flags: dict[str, list[str]] = {}
        for name, value in _FLAG_TOKEN_RE.findall(profile):
            if name.startswith("dpi-desync"):
                flags.setdefault(name, []).append(value)
        result.append(flags)
    return result


def _adapted_profiles_on_disk(filename: str) -> list[dict[str, list[str]]]:
    text = (STRATEGIES_DIR / filename).read_text(encoding="utf-8")
    match = _WINWS_ARGS_RE.search(text)
    assert match, f"{filename}: could not locate the winws.exe argument block"
    return _profiles_as_flag_maps(match.group(1))


@pytest.mark.parametrize("original_name,adapted_name", sorted(KNOWN_ADAPTATIONS.items()))
def test_adapter_reproduces_the_known_strategy(original_name, adapted_name):
    original_text = (FIXTURES / original_name).read_text(encoding="utf-8")

    result = adapt.adapt_strategy(original_name, original_text)

    assert result.ok, f"{original_name}: adaptation failed - {result.reason}"
    generated_profiles = _profiles_as_flag_maps(
        _WINWS_ARGS_RE.search(result.content).group(1)
    )
    on_disk_profiles = _adapted_profiles_on_disk(adapted_name)

    assert generated_profiles == on_disk_profiles, (
        f"{original_name} -> {adapted_name}: adapter picked different dpi-desync "
        f"flags than what BridgeBox ships"
    )


# ---- selection failure is refused, not guessed -----------------------------


def test_a_strategy_with_no_80_443_tcp_profile_is_refused():
    text = (
        '"%BIN%winws.exe" --filter-tcp=2053,2083 --hostlist-domains=discord.media '
        '--dpi-desync=multisplit --new '
        '--filter-udp=443 --hostlist="%LISTS%x.txt" --dpi-desync=fake '
        '--dpi-desync-fake-quic="%BIN%quic.bin"'
    )

    result = adapt.adapt_strategy("general (weird).bat", text)

    assert not result.ok
    assert "TCP" in result.reason


def test_a_strategy_with_no_quic_profile_is_refused():
    text = (
        '"%BIN%winws.exe" --filter-tcp=80,443 --hostlist="%LISTS%x.txt" '
        '--dpi-desync=multisplit --dpi-desync-split-pos=1 --new '
        '--filter-udp=19294 --filter-l7=discord --dpi-desync=fake '
        '--dpi-desync-fake-discord="%BIN%discord.bin"'
    )

    result = adapt.adapt_strategy("general (weird).bat", text)

    assert not result.ok
    assert "QUIC" in result.reason


def test_refuses_when_a_referenced_bin_is_not_among_the_available_payloads():
    text = (
        '"%BIN%winws.exe" --filter-tcp=80,443 --hostlist="%LISTS%x.txt" '
        '--dpi-desync=multisplit --dpi-desync-split-seqovl-pattern="%BIN%new_pattern.bin" --new '
        '--filter-udp=443 --hostlist="%LISTS%x.txt" --dpi-desync=fake '
        '--dpi-desync-fake-quic="%BIN%quic_initial_www_google_com.bin"'
    )

    result = adapt.adapt_strategy(
        "general (weird).bat", text, available_bins={"quic_initial_www_google_com.bin"}
    )

    assert not result.ok
    assert "new_pattern.bin" in result.reason


# ---- the actual trust boundary: a hostile value cannot break out of the ---
# ---- template's quoted argument and inject a batch command ---------------


@pytest.mark.parametrize(
    "hostile_value",
    [
        '"&calc.exe&"',          # closes the quote, chains a command
        '"|calc.exe',            # pipe
        "value\r\ndel /s /q C:\\",  # embedded newline, a second batch line
        "%WINDIR%\\evil.bat",    # arbitrary env-var expansion, not %BIN%
        "<redirected",
    ],
)
def test_a_hostile_dpi_desync_value_never_reaches_the_rendered_bat(hostile_value):
    text = (
        f'"%BIN%winws.exe" --filter-tcp=80,443 --hostlist="%LISTS%x.txt" '
        f'--dpi-desync=multisplit --dpi-desync-fake-http={hostile_value} --new '
        '--filter-udp=443 --hostlist="%LISTS%x.txt" --dpi-desync=fake '
        '--dpi-desync-fake-quic="%BIN%quic_initial_www_google_com.bin"'
    )

    result = adapt.adapt_strategy("general (weird).bat", text)

    assert result.ok
    assert "calc.exe" not in result.content
    assert "del /s" not in result.content
    assert "WINDIR" not in result.content
    assert "redirected" not in result.content


# ---- the aggressive-strategy warning survives regeneration ----------------


_SYNDATA_TEXT = (
    '"%BIN%winws.exe" --filter-tcp=80,443 --hostlist="%LISTS%x.txt" '
    '--dpi-desync=syndata,multidisorder --new '
    '--filter-udp=443 --hostlist="%LISTS%x.txt" --dpi-desync=fake '
    '--dpi-desync-fake-quic="%BIN%quic_initial_www_google_com.bin"'
)

_FOOLED_TEXT = (
    '"%BIN%winws.exe" --filter-tcp=80,443 --hostlist="%LISTS%x.txt" '
    '--dpi-desync=fake,multisplit --dpi-desync-fooling=ts --new '
    '--filter-udp=443 --hostlist="%LISTS%x.txt" --dpi-desync=fake '
    '--dpi-desync-fake-quic="%BIN%quic_initial_www_google_com.bin"'
)


def test_a_syndata_profile_gets_the_warning_comment_on_every_regeneration():
    """The bug this guards: a hand-added "NOT RECOMMENDED" comment on
    Alternative 5.bat was erased by the very next regeneration, because
    render_bat() rewrites the whole header unconditionally. Deriving the
    warning from the profile itself means there is nothing left to forget."""
    result = adapt.adapt_strategy("general (ALT5).bat", _SYNDATA_TEXT)

    assert result.ok
    assert "NOT RECOMMENDED" in result.content


def test_a_fooled_profile_gets_no_warning_comment():
    result = adapt.adapt_strategy("general (ALT11).bat", _FOOLED_TEXT)

    assert result.ok
    assert "NOT RECOMMENDED" not in result.content


def test_suggest_filename_maps_known_qualifier_families_to_their_pretty_name():
    """The bug this guards: without this mapping, an update recognized none
    of these by filename and added every one as a duplicate of a strategy
    BridgeBox already ships under its hand-picked name."""
    assert adapt.suggest_filename("general (ALT13).bat") == "Alternative 13.bat"
    assert adapt.suggest_filename("general (ALT).bat") == "Alternative 1.bat"
    assert adapt.suggest_filename("general (ALT2).bat") == "Alternative 2.bat"
    assert adapt.suggest_filename("general (SIMPLE FAKE).bat") == "Simple Fake.bat"
    assert adapt.suggest_filename("general (SIMPLE FAKE ALT).bat") == "Simple Fake 1.bat"
    assert adapt.suggest_filename("general (SIMPLE FAKE ALT2).bat") == "Simple Fake 2.bat"
    assert adapt.suggest_filename("general (FAKE TLS AUTO).bat") == "Fake TLS Auto.bat"
    assert adapt.suggest_filename("general (FAKE TLS AUTO ALT).bat") == "Fake TLS Auto 1.bat"
    assert adapt.suggest_filename("general (FAKE TLS AUTO ALT3).bat") == "Fake TLS Auto 3.bat"


def test_suggest_filename_keeps_an_unseen_qualifier_verbatim():
    assert adapt.suggest_filename("general (EXP).bat") == "General (EXP).bat"
    assert adapt.suggest_filename("general (WEIRD).bat") == "General (WEIRD).bat"
    assert adapt.suggest_filename("general.bat") == "General.bat"


_STAMPABLE = (
    '"%BIN%winws.exe" --filter-tcp=80,443 --hostlist="%LISTS%x.txt" '
    '--dpi-desync=multisplit --new '
    '--filter-udp=443 --hostlist="%LISTS%x.txt" --dpi-desync=fake '
    '--dpi-desync-fake-quic="%BIN%quic.bin"'
)


def test_stamp_round_trips_and_detects_an_edit():
    """The stamp is what lets an update tell its own previous output from a
    file the user has since edited - the only thing standing between a release
    and somebody's hand-tuned strategy."""
    content = adapt.adapt_strategy("general.bat", _STAMPABLE).content

    stamped = adapt.stamp(content, "1.10.1")

    assert adapt.is_unmodified_generated(stamped)
    assert stamped.startswith(content), "the stamp must only append"
    assert "1.10.1" in stamped.rstrip("\n").rsplit("\n", 1)[-1]

    edited = stamped.replace("--dpi-desync=multisplit", "--dpi-desync=disorder", 1)
    assert not adapt.is_unmodified_generated(edited)


def test_an_unstamped_file_is_never_mistaken_for_generated():
    """All 21 strategies shipped today are unstamped. Reading one as generated
    would mean silently overwriting a hand adaptation."""
    assert not adapt.is_unmodified_generated("@echo off\n:: hand written\n")
    assert not adapt.is_unmodified_generated("")
    # Present but malformed - a truncated or hand-typed stamp line.
    assert not adapt.is_unmodified_generated("@echo off\n:: BridgeBox-stamp 1.10.1\n")


def test_the_stamp_does_not_disturb_what_the_other_tests_read():
    """The stamp goes after `endlocal`, so the winws invocation the golden test
    and test_strategy_assets parse is byte-identical with or without it."""
    content = adapt.adapt_strategy("general.bat", _STAMPABLE).content
    stamped = adapt.stamp(content, "1.10.1")

    assert _WINWS_ARGS_RE.search(content).group(1) == _WINWS_ARGS_RE.search(stamped).group(1)
