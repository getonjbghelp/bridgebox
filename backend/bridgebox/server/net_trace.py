"""Detailed DNS/connect/reuse timing for every outbound request the bridge's
shared aiohttp.ClientSession makes - REST forwarding (rooms.py) and WS
upstream connects (relay.py/blobcast.py) alike, since a WS handshake is
itself one HTTP request as far as aiohttp's tracing is concerned.

Complements rooms.py's request/response logging (which covers WHAT was
sent and its total round-trip) with WHEN each phase happened: DNS resolve,
TCP+TLS connect vs. reusing an already-warm pooled connection. This session's
Blobcast investigation kept having to reconstruct connection-phase timing by
hand from access-log timestamps - aiohttp.TraceConfig is the one place that
actually sees these events, no amount of logging around the call site could
observe them from outside.

All at DEBUG - the level Settings' logging control already exposes, and the
one every other verbose per-request line in this codebase already uses.
"""
from __future__ import annotations

import logging
import time

import aiohttp

from .rooms import redact

logger = logging.getLogger(__name__)


def build_trace_config() -> aiohttp.TraceConfig:
    config = aiohttp.TraceConfig()

    async def on_request_start(session, ctx, params) -> None:
        ctx.bb_start = time.monotonic()
        ctx.bb_method = params.method
        # Redacted once here and reused by every later hook for this same
        # request - the room token travels in the query string, and this is
        # the one place raw enough to still carry it.
        ctx.bb_url = redact(str(params.url))

    async def on_dns_resolvehost_start(session, ctx, params) -> None:
        ctx.bb_dns_start = time.monotonic()

    async def on_dns_resolvehost_end(session, ctx, params) -> None:
        elapsed_ms = (time.monotonic() - ctx.bb_dns_start) * 1000
        logger.debug("[net] DNS resolved %s in %.0fms", params.host, elapsed_ms)

    async def on_connection_create_start(session, ctx, params) -> None:
        ctx.bb_connect_start = time.monotonic()

    async def on_connection_create_end(session, ctx, params) -> None:
        elapsed_ms = (time.monotonic() - ctx.bb_connect_start) * 1000
        logger.debug(
            "[net] %s %s: new connection established in %.0fms",
            ctx.bb_method,
            ctx.bb_url,
            elapsed_ms,
        )

    async def on_connection_reuseconn(session, ctx, params) -> None:
        # This is the line that actually proves runtime_core.py's upstream
        # pre-warm did something: without it, every request pays the "new
        # connection" cost above instead.
        logger.debug("[net] %s %s: reused a pooled connection", ctx.bb_method, ctx.bb_url)

    async def on_request_end(session, ctx, params) -> None:
        elapsed_ms = (time.monotonic() - ctx.bb_start) * 1000
        logger.debug(
            "[net] %s %s -> HTTP %s in %.0fms total",
            ctx.bb_method,
            ctx.bb_url,
            params.response.status,
            elapsed_ms,
        )

    async def on_request_exception(session, ctx, params) -> None:
        elapsed_ms = (time.monotonic() - ctx.bb_start) * 1000
        logger.debug(
            "[net] %s %s failed after %.0fms: %r",
            ctx.bb_method,
            ctx.bb_url,
            elapsed_ms,
            params.exception,
        )

    config.on_request_start.append(on_request_start)
    config.on_dns_resolvehost_start.append(on_dns_resolvehost_start)
    config.on_dns_resolvehost_end.append(on_dns_resolvehost_end)
    config.on_connection_create_start.append(on_connection_create_start)
    config.on_connection_create_end.append(on_connection_create_end)
    config.on_connection_reuseconn.append(on_connection_reuseconn)
    config.on_request_end.append(on_request_end)
    config.on_request_exception.append(on_request_exception)
    return config
