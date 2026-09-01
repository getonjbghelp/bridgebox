from __future__ import annotations

import asyncio
import html
import json
import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable

import aiohttp

from .server.rooms import FALLBACK_USER_AGENT
from .zapret.process import ZapretProcess, console_flags
from .zapret.strategies import Strategy, resolve_strategy

logger = logging.getLogger(__name__)

# Without a per-request cap a DPI-blocked target doesn't fail - it hangs until
# aiohttp's 5-minute default, so a single bad strategy could outlast the whole
# suite's budget and the popup would show nothing at all.
PROBE_TIMEOUT_S = 8.0

# winws.exe/WinDivert needs a moment after launch before the filter is actually
# in the packet path. Probing immediately measures a half-initialized strategy
# and reports it as slow or broken.
# ponytail: fixed delay, not a readiness check - winws has no ready signal to
# poll. Raise it if a strategy tests worse here than it performs in practice.
STRATEGY_SETTLE_S = 1.5


def describe_exception(exc: BaseException) -> str:
    """Render an exception the way a user can act on. Bare str(exc) is an
    empty string for whole classes of failures - asyncio.TimeoutError being
    the one that matters here - which is how a real timeout used to reach the
    UI as a blank error with no indication anything had gone wrong."""
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__

# A real API endpoint, not the bare host: the bare host is answered by the AWS
# load balancer with a 403 without ever reaching Ecast, so timing it would
# measure the wrong hop. A nonexistent room code returns a fast, small JSON
# 404 from the real service - exactly the round trip worth measuring.
PROBE_URL = "https://ecast.jackboxgames.com/api/v2/rooms/ZZZZ"

ProbeFn = Callable[[], Awaitable[float]]
SwitchFn = Callable[[str], Awaitable[None]]


def build_probe(session, *, url: str = PROBE_URL) -> ProbeFn:
    """Build a zero-arg probe (bench.py's ProbeFn) that times an HTTPS
    request to the real Jackbox API through the given session - used for
    Settings' "Тест стратегий" ping mode. A short-lived session is expected
    per diagnostics run (independent of whether the bridge itself is up),
    so a strategy test works even before the user has toggled the bridge on."""

    async def probe() -> float:
        start = time.monotonic()
        async with session.get(url, headers={"User-Agent": FALLBACK_USER_AGENT}) as response:
            await response.read()
        return (time.monotonic() - start) * 1000

    return probe


# ecast.jackboxgames.com is the API entry point (room creation); the actual
# relay a room gets assigned to is a separate per-shard host - confirmed
# against live traffic as e.g. "ecast-prod-use2.jackboxgames.com" (see
# rooms.py's "host" handling). Both are worth testing per strategy: a
# strategy that reaches the API but not the assigned relay shard would still
# leave the game unable to connect. jackbox.tv (previously tested here) is
# just the static player-facing site - unrelated to the Ecast traffic this
# bridge actually proxies, so a result for it told the user nothing
# actionable about whether a strategy works for the game itself.
ECAST_TARGETS: list[tuple[str, str]] = [
    ("ecast.jackboxgames.com", PROBE_URL),
    ("ecast-prod-use2.jackboxgames.com", "https://ecast-prod-use2.jackboxgames.com/"),
]

# blobcast.jackboxgames.com is Blobcast's own API entry point (Party Pack
# 1-6's GET /room, see server/blobcast.py). Root path, same reasoning as
# ecast-prod-use2 above: there is no confirmed lightweight endpoint to probe
# instead, and any HTTP response - even a 404 - proves the DPI bypass got the
# TLS handshake through, which is all a ping measures.
#
# Deliberately just the one host, unlike ECAST_TARGETS - a relay-shard entry
# (ecast-prod-use1.jackboxgames.com) was here too, but per-strategy Blobcast
# testing is meant to stay scoped to this single address.
BLOBCAST_TARGETS: list[tuple[str, str]] = [
    ("blobcast.jackboxgames.com", "https://blobcast.jackboxgames.com/"),
]


def _profile_targets(profile) -> list[tuple[str, str]]:
    """Targets for a non-official profile's own upstream - a mirror or
    self-hosted server whose API shape isn't confirmed ahead of time, unlike
    ECAST_TARGETS/BLOBCAST_TARGETS' known-good endpoints. Probes the bare
    upstream root instead - the same reasoning BLOBCAST_TARGETS' single entry
    already relies on: any HTTP response, even a 404, proves the DPI bypass
    got the TLS handshake through, which is all a ping measures."""
    from urllib.parse import urlsplit

    host = urlsplit(profile.upstream).hostname or profile.upstream
    return [(host, f"{profile.upstream}/")]


def targets_for(kind: str, profiles) -> list[tuple[str, str]]:
    """ECAST_TARGETS/BLOBCAST_TARGETS, unless the user has pointed this
    protocol at a profile of their own - then every ping/probe in this app
    (the strategy suite, "Проверить соединение" on the Home screen) targets
    THAT server instead. Reaching the official servers proves nothing about
    whether a strategy or a network path reaches a different one, which is
    the whole point of testing against the server actually in use.

    `profiles` is a config.ProfilesConfig, not imported/type-hinted here so
    this module keeps no dependency on config.py - anything with an
    `.active(kind)` returning an object with `.upstream`/`.builtin` duck-types
    fine, and both real callers already have one on hand (self._config.profiles)."""
    profile = profiles.active(kind)
    if not profile.builtin:
        return _profile_targets(profile)
    return ECAST_TARGETS if kind == "ecast" else BLOBCAST_TARGETS


async def probe_targets(
    session,
    targets: list[tuple[str, str]] = ECAST_TARGETS,
    *,
    timeout_s: float = PROBE_TIMEOUT_S,
) -> dict:
    """Time a request to each target independently - one blocked target
    doesn't prevent reporting on the others. Targets are probed concurrently:
    they're independent measurements, and doing them in series meant every
    blocked target's timeout added to the suite's total wall time."""

    async def probe_one(name: str, url: str) -> tuple[str, dict]:
        start = time.monotonic()
        logger.debug("probing %s (%s), timeout=%.1fs", name, url, timeout_s)
        try:
            async with session.get(
                url,
                headers={"User-Agent": FALLBACK_USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=timeout_s),
            ) as response:
                body = await response.read()
                elapsed = (time.monotonic() - start) * 1000
                logger.info(
                    "probe %s: HTTP %s in %.0fms (%d bytes, %s)",
                    name,
                    response.status,
                    elapsed,
                    len(body),
                    response.headers.get("Content-Type", "?"),
                )
                return name, {
                    "ok": True,
                    "elapsedMs": elapsed,
                    "status": response.status,
                    "error": None,
                }
        except Exception as exc:
            logger.warning(
                "probe %s failed after %.0fms: %s",
                name,
                (time.monotonic() - start) * 1000,
                describe_exception(exc),
            )
            return name, {
                "ok": False,
                "elapsedMs": None,
                "status": None,
                "error": describe_exception(exc),
            }

    pairs = await asyncio.gather(*(probe_one(name, url) for name, url in targets))
    return dict(pairs)


async def run_strategy_suite(
    strategies,
    *,
    switch: SwitchFn,
    session_factory,
    targets: list[tuple[str, str]] = ECAST_TARGETS,
    settle_s: float = STRATEGY_SETTLE_S,
    on_result: Callable[[dict], None] | None = None,
) -> list[dict]:
    """Cycle Zapret through each strategy, probing every target for each one
    - the multi-target version of zapret/bench.py's single-probe engine,
    built for Settings' "Тест стратегий" results popup (modeled on the
    Flowseal test-all-configs script: switch, probe, record, move on).

    on_result is called with each strategy's entry the moment it's known, so
    the UI can fill the table in as the run proceeds instead of waiting for
    the whole suite - a full suite takes minutes, which is far longer than a
    user will stare at a blank popup.

    Cancellation (user closes the popup) propagates out of the awaits here;
    stopping Zapret afterwards is the caller's job, since only the caller
    knows what should be running once the test is over.

    `strategies` arrives pre-filtered - "Тестировать всё"/skip_heavy is
    decided once, by desktop.Api.test_strategies before it ever calls in
    here, not re-decided per stage."""
    results: list[dict] = []

    def record(entry: dict) -> None:
        results.append(entry)
        if on_result is not None:
            on_result(entry)

    queued = list(strategies)
    logger.info(
        "strategy suite: %d strategies to test (settle=%.1fs, targets=%s)",
        len(queued),
        settle_s,
        ", ".join(name for name, _ in targets),
    )

    for index, strategy in enumerate(queued, start=1):
        name = strategy.filename.removesuffix(".bat")
        logger.info("[%d/%d] switching to %s", index, len(queued), name)
        try:
            await switch(strategy.key)
        except Exception as exc:
            logger.error("[%d/%d] switch to %s failed: %s", index, len(queued), name, exc)
            record(
                {
                    "key": strategy.key,
                    "name": name,
                    "ok": False,
                    "targets": {},
                    "error": describe_exception(exc),
                }
            )
            continue

        if settle_s:
            logger.debug("waiting %.1fs for WinDivert to enter the packet path", settle_s)
            await asyncio.sleep(settle_s)

        async with session_factory() as session:
            target_results = await probe_targets(session, targets)

        ok = any(t["ok"] for t in target_results.values())
        logger.info(
            "[%d/%d] %s -> %s (%s)",
            index,
            len(queued),
            name,
            "ok" if ok else "все цели недоступны",
            ", ".join(
                f"{target}={'%.0fms' % r['elapsedMs'] if r['ok'] else 'fail'}"
                for target, r in target_results.items()
            ),
        )
        record(
            {
                "key": strategy.key,
                "name": name,
                "ok": ok,
                "targets": target_results,
                "error": None if ok else "все цели недоступны",
            }
        )
    logger.info("strategy suite finished: %d results", len(results))
    return results


def build_switch(
    zapret_process: ZapretProcess, strategies: dict[str, Strategy], *, hide_console: bool = True
) -> SwitchFn:
    """Build a switch (bench.py's SwitchFn) that stops whatever strategy is
    currently running and starts the requested one - reuses the same
    ZapretProcess/strategies machinery RuntimeCore.start() already resolves,
    no separate zapret-control code path.

    `hide_console` defaults to True (BridgeBox's own shipped default,
    ZapretConfig.hide_console) rather than to start()'s own default of a
    visible console - a caller that forgets to pass it should get the quiet
    behaviour everyone actually wants, not a console flashing on screen once
    per strategy for however many minutes the suite runs. Same
    console_flags()/capture_output pairing RuntimeCore._start() uses, so a
    hidden console's output still reaches the Logs screen instead of going
    nowhere."""

    async def switch(key: str) -> None:
        if zapret_process.is_running:
            zapret_process.stop()
        strategy = resolve_strategy(key, strategies)
        zapret_process.start(
            strategy.path,
            creationflags=console_flags(hide_console),
            capture_output=hide_console,
        )

    return switch


def render_strategy_results_json(results: list[dict]) -> str:
    """Serialise a strategy-test run for export.

    Takes whatever desktop.Api._strategy_results holds when the user clicks
    export - the same list test_strategies_progress() has been streaming to
    the popup - so exporting never re-runs the suite or needs the frontend to
    round-trip its own copy back over the bridge."""
    payload = {
        "format": "bridgebox-strategy-test",
        "version": 1,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _html_escape(text: str) -> str:
    return html.escape(text, quote=True)


def render_strategy_results_html(results: list[dict]) -> str:
    """A self-contained report, openable by double-clicking - no server, no
    build step, the same constraint every other exported artifact in this app
    already meets (profile export, config: plain files that need nothing but
    a text editor or, here, a browser)."""
    target_names: list[str] = []
    for entry in results:
        for name in entry.get("targets") or {}:
            if name not in target_names:
                target_names.append(name)

    def cell(entry: dict, name: str) -> str:
        target = (entry.get("targets") or {}).get(name)
        if not target:
            return "—"
        if not target.get("ok"):
            reason = _html_escape(str(target.get("error") or ""))
            return f'<span class="fail" title="{reason}">✕</span>'
        elapsed = target.get("elapsedMs")
        return f"{elapsed:.0f} мс" if elapsed is not None else "—"

    has_stage = any(entry.get("targetSet") for entry in results)
    rows = []
    for entry in results:
        stage_cell = f"<td>{_html_escape(str(entry.get('targetSet') or ''))}</td>" if has_stage else ""
        cells = "".join(f"<td>{cell(entry, name)}</td>" for name in target_names)
        status = "ok" if entry.get("ok") else "fail"
        rows.append(
            f'<tr class="{status}">{stage_cell}'
            f"<td>{_html_escape(str(entry.get('name') or entry.get('key') or '?'))}</td>"
            f"{cells}<td>{_html_escape(str(entry.get('error') or ''))}</td></tr>"
        )

    stage_header = "<th>Набор</th>" if has_stage else ""
    target_headers = "".join(f"<th>{_html_escape(name)}</th>" for name in target_names)
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    return (
        "<!doctype html>\n"
        '<html lang="ru"><head><meta charset="utf-8">\n'
        "<title>BridgeBox — тест стратегий</title>\n"
        "<style>\n"
        '  body { font: 14px/1.5 -apple-system, "Segoe UI", system-ui, sans-serif; '
        "margin: 32px; color: #0b1220; }\n"
        "  h1 { font-size: 18px; }\n"
        "  table { border-collapse: collapse; width: 100%; }\n"
        "  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid #e2e7ef; "
        "font-size: 13px; }\n"
        "  th { background: #eef1f6; text-transform: uppercase; font-size: 11px; color: #5b6472; }\n"
        "  tr.fail td { color: #b91c1c; }\n"
        "  .fail { color: #b91c1c; }\n"
        "  p.meta { color: #8b93a1; font-size: 12px; }\n"
        "</style></head>\n"
        "<body>\n"
        "<h1>BridgeBox — результаты теста стратегий</h1>\n"
        f'<p class="meta">Сформировано: {generated} · стратегий: {len(results)}</p>\n'
        "<table>\n"
        f"<thead><tr>{stage_header}<th>Стратегия</th>{target_headers}<th>Ошибка</th></tr></thead>\n"
        f"<tbody>\n{''.join(rows)}\n</tbody>\n"
        "</table>\n"
        "</body></html>\n"
    )
