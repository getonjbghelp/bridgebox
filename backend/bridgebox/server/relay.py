from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Awaitable, Callable, Protocol

import aiohttp
from aiohttp import web

from .rooms import ROOM_ID_KEYS as ROOM_QUERY_KEYS
from .rooms import redact

logger = logging.getLogger(__name__)

FallbackHandler = Callable[[web.Request], Awaitable[web.StreamResponse]]


async def _default_fallback(request: web.Request) -> web.Response:
    return web.Response(status=400, text="not a websocket upgrade request")

_STOP_TYPES = {
    aiohttp.WSMsgType.CLOSE,
    aiohttp.WSMsgType.CLOSING,
    aiohttp.WSMsgType.CLOSED,
    aiohttp.WSMsgType.ERROR,
}

# Long enough that a full Ecast message is usually readable in one line,
# capped so a single state dump doesn't bury the surrounding traffic.
FRAME_PREVIEW_CHARS = 800

# RFC 6455: a WS control frame (close included) is capped at 125 bytes total,
# 2 of which are the status code - 123 is the hard ceiling for the reason,
# not a stylistic choice. Truncating well below that (as a 100-*character*
# cap did here previously) cut the failure reason - including the upstream
# URL - mid-string for no reason, which is what showed up truncated in the
# UI's error message.
WS_CLOSE_REASON_MAX_BYTES = 123


def _close_reason_bytes(text: str) -> bytes:
    """Encode `text` for a WS close frame, truncated to the protocol's byte
    limit without splitting a multi-byte UTF-8 character in half."""
    encoded = text.encode("utf-8")
    if len(encoded) <= WS_CLOSE_REASON_MAX_BYTES:
        return encoded
    # Trim byte-by-byte from the end until decoding succeeds, rather than
    # slicing to a fixed byte count that could land inside a multi-byte
    # sequence (str.encode() has no "best-effort" truncation of its own).
    truncated = encoded[:WS_CLOSE_REASON_MAX_BYTES]
    while truncated:
        try:
            truncated.decode("utf-8")
            return truncated
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    return b""


def _frame_preview(data: str, limit: int = FRAME_PREVIEW_CHARS) -> str:
    # Redacted like HTTP bodies are: the WS stream carries the same room
    # token, and every frame is logged at debug level. Redact the WHOLE
    # frame before truncating - the same truncate-before-redact ordering as
    # rooms._preview leaked a partial token when the cut landed inside one;
    # see test_a_frame_preview_has_the_same_truncate_then_redact_ordering.
    text = redact(data)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... (+{len(data) - limit} chars)"


class WsLike(Protocol):
    """Minimal shape shared by aiohttp's server-side WebSocketResponse and
    client-side ClientWebSocketResponse - both work with pump()/relay()
    unmodified."""

    closed: bool

    async def send_str(self, data: str) -> None: ...
    async def close(self) -> None: ...
    def __aiter__(self): ...


class WsConnector(Protocol):
    async def connect(self, url: str, *, headers: dict[str, str] | None = None) -> WsLike: ...


# The handshake only, not the session: a WS relay is expected to stay open for
# the whole game, so a total timeout would kill a perfectly healthy one. What
# needs bounding is the connect, which had no cap at all and fell back to
# aiohttp's five-minute default - the same failure mode rooms.py already
# documents for the HTTP path, where a DPI-blocked upstream hangs instead of
# failing fast.
WS_CONNECT_TIMEOUT_S = 15.0


class AiohttpWsConnector:
    """Production WsConnector backed by a real aiohttp.ClientSession."""

    def __init__(self, session: aiohttp.ClientSession):
        self._session = session

    async def connect(self, url: str, *, headers: dict[str, str] | None = None) -> WsLike:
        # `headers` is optional so the Ecast relay, which passes none, keeps
        # exactly the behaviour it had. Blobcast needs it: opening the
        # upstream socket with no headers meant Jackbox saw aiohttp's own
        # "Python/3.x aiohttp/..." User-Agent and answered 403 - the same
        # non-browser-User-Agent rejection rooms.py documents for the HTTP
        # path - so the room could never be created.
        #
        # asyncio.timeout, not ws_connect(timeout=...): ClientWSTimeout only
        # carries ws_receive/ws_close, so it bounds frames on an established
        # socket and does nothing for the handshake (checked against aiohttp
        # 3.14, not assumed). Wrapping the await is what actually caps the
        # connect, and it ends there - the relay itself must be free to stay
        # open for the whole game.
        async with asyncio.timeout(WS_CONNECT_TIMEOUT_S):
            return await self._session.ws_connect(url, headers=headers)


async def pump(
    source: WsLike, sink: WsLike, *, label: str = "ws", level: int | None = None
) -> None:
    """Forward text frames from source to sink until source ends or signals
    close. Ecast v2 never uses binary WS frames (documented), so non-text
    application data is intentionally not forwarded.

    `level=None` means the frames are NOT logged at all, and that is the
    default. It used to be logging.DEBUG, which looked like "off" and was not:
    setup_logging deliberately hands the Logs screen every record regardless of
    the configured level, because the screen has its own DEBUG pill. So
    `log_frames: false` kept the game's whole protocol out of the FILE while
    streaming it into the UI - 1858 frame lines in one session's log, with the
    setting off the entire time.

    Turning it on is what reading the protocol costs. Frames pass through
    unparsed either way - logging is strictly an observer here."""
    frames = 0
    chars = 0
    async for msg in source:
        if msg.type == aiohttp.WSMsgType.TEXT:
            frames += 1
            chars += len(msg.data)
            if level is not None:
                logger.log(
                    level,
                    "[%s] frame #%d (%d chars): %s",
                    label,
                    frames,
                    len(msg.data),
                    _frame_preview(msg.data),
                )
            await sink.send_str(msg.data)
        elif msg.type in _STOP_TYPES:
            logger.info("[%s] stop frame %s after %d frames", label, msg.type.name, frames)
            break
        elif level is not None:
            # Binary/ping/pong - not forwarded by design, but worth knowing
            # about if the protocol ever starts using them.
            logger.log(level, "[%s] ignored non-text frame: %s", label, msg.type.name)
    logger.info("[%s] ended: %d frames, %d chars forwarded", label, frames, chars)


async def relay(
    ws_a: WsLike,
    ws_b: WsLike,
    *,
    label_a: str = "game->upstream",
    label_b: str = "upstream->game",
    # None = do not log frame contents. See pump() for why that is the default.
    level: int | None = None,
) -> None:
    """Pump both directions concurrently between two already-open WS
    connections until either side ends, then close both."""
    task_a = asyncio.create_task(pump(ws_a, ws_b, label=label_a, level=level))
    task_b = asyncio.create_task(pump(ws_b, ws_a, label=label_b, level=level))

    _, pending = await asyncio.wait({task_a, task_b}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    for ws in (ws_a, ws_b):
        if not ws.closed:
            await ws.close()


def resolve_room(query, room_relays: dict[str, str]) -> tuple[str | None, str | None]:
    """Find which upstream relay a WS connection belongs to.

    Tries the known room-code parameter spellings first. Failing that, falls
    back to the single known room: BridgeBox is a local single-session bridge,
    so when exactly one room has been registered there is no ambiguity about
    where the connection belongs - and refusing it over a parameter-name
    mismatch would break the game for no protective benefit."""
    for key in ROOM_QUERY_KEYS:
        value = query.get(key)
        if value and value in room_relays:
            return value, room_relays[value]

    if len(room_relays) == 1:
        room_id, relay_url = next(iter(room_relays.items()))
        logger.info(
            "WS room not identified from query params; using the only known room (%s)", room_id
        )
        return room_id, relay_url

    return None, None


def create_relay_handler(
    room_relays: dict[str, str],
    connector: WsConnector,
    *,
    fallback: FallbackHandler = _default_fallback,
):
    """Build the GET handler this route now claims on every path (see
    register_relay_route): looks up which real relay a room was assigned to
    (recorded by RoomsProxy), opens a matching upstream WS connection, and
    relays frames 1:1 in both directions without parsing them.

    Requests that aren't an actual WS handshake are handed to `fallback`
    (normally the browser-warning stub) instead of being treated as a relay
    candidate - see register_relay_route for why this claims every path."""

    async def handle_ws(request: web.Request) -> web.StreamResponse:
        if request.headers.get("Upgrade", "").lower() != "websocket":
            return await fallback(request)

        # SECURITY FIX (H3): the WS query string carries the same room token
        # the HTTP path does. Redacted once and reused, so a log line added
        # later cannot reach for the raw value by accident.
        safe_query = redact(request.query_string)
        logger.info("WS upgrade: path=%s query=%s", request.path, safe_query)

        room_id, real_base = resolve_room(request.query, room_relays)
        if not real_base:
            logger.warning(
                "WS rejected: no upstream relay known (query=%s, known rooms=%s). "
                "The room's API call must go through the bridge before its WS connects.",
                safe_query,
                list(room_relays.keys()),
            )
            return web.Response(status=404, text="unknown room")

        logger.info("WS relay opening (room=%s) -> %s", room_id, real_base)
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        upstream_url = (
            f"{real_base}?{request.query_string}" if request.query_string else real_base
        )
        try:
            upstream_ws = await connector.connect(upstream_url)
        except Exception as exc:
            logger.error("WS upstream connect failed (room=%s): %s", room_id, exc)
            # A bare ws.close() here left the client-facing handshake looking
            # identical to a normal, successful close - test_connection was
            # reporting "ok" on this path without ever knowing the upstream
            # never connected. INTERNAL_ERROR + a reason the caller can check
            # (ws.close_code / the close frame's message) makes the failure
            # visible on the client side instead of silently swallowed here.
            await ws.close(
                code=aiohttp.WSCloseCode.INTERNAL_ERROR,
                message=_close_reason_bytes(str(exc)),
            )
            return ws

        # SECURITY FIX (H3): upstream_url is real_base + the raw query string.
        logger.info("WS relay connected upstream (room=%s) -> %s", room_id, redact(upstream_url))
        try:
            await relay(
                ws,
                upstream_ws,
                label_a=f"{room_id} game->upstream",
                label_b=f"{room_id} upstream->game",
            )
        finally:
            logger.info("WS relay closed (room=%s)", room_id)
        return ws

    return handle_ws


def register_relay_route(
    app: web.Application,
    room_relays: dict[str, str],
    connector: WsConnector,
    *,
    fallback: FallbackHandler = _default_fallback,
) -> None:
    """Claims every GET path, not just "/ws". Confirmed against the real
    room-creation response (see rooms.py's "host" handling): the game builds
    its own WS URL client-side from the rewritten host, and the exact path
    convention it uses beyond that isn't confirmed - a community server for
    the same game (johnbox) doesn't check path either, matching purely on
    the role/roomId query params. Must be registered before the app's
    catch-all, which also matches every path - see factory.py."""
    app.router.add_get("/{tail:.*}", create_relay_handler(room_relays, connector, fallback=fallback))
