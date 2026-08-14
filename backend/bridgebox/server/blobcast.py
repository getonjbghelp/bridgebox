"""Blobcast ("API v1") - the protocol Party Packs 1-6 and several singles use.

Ecast and Blobcast coexist on this one listener without anything to switch,
because their paths are disjoint: Ecast is /api/v2/*, Blobcast is /room,
/accessToken and /socket.io/*. Nothing here touches the Ecast path.

There is no reference implementation to copy. The best-known private server,
InvoxiPlayGames/johnbox, lists "Blobcast / API v1" under *Unimplemented
features* and says so again in its README ("...uses socketio for WebSockets
and is currently not supported"). The protocol is therefore being read off
live traffic, which is what this module exists to make possible.

HOW THE INTERCEPTION WORKS
--------------------------
GET /room answers with a BARE HOSTNAME in "server", and the game takes its
whole socket.io session there - which is why nothing past POST /accessToken
was ever visible here. Three attempts to point that field back at the bridge
failed before a packet capture explained them: the game uses the value as a
hostname and appends port 38203 itself, so anything carrying a port or a
scheme became an unresolvable name (each stalling the game on its own
repeatable delay - 5.13s, 7.35s, 8.67s - with nothing reaching us at all).

So the field becomes "localhost", which resolves to 127.0.0.1 with no
hosts-file edit and is already a SAN on the bridge's certificate, and the
session lands on a second site listening on SOCKETIO_PORT. Frames are
relayed verbatim and logged; the protocol is read off them, not guessed.

Two things had to be true before it worked, both of them measured:
the game sends NO User-Agent on the WS upgrade and Jackbox refuses aiohttp's
own (see WS_FORWARDED_HEADERS), and port 38203 is outside the zapret
strategies' --wf-tcp filter, so the session had no DPI bypass at all until
the port was added there. Confirmed live afterwards: a full round with three
players, 970 frames relayed over about fifteen minutes.
"""
from __future__ import annotations

import json
import logging
import re

from aiohttp import web

from .guard import MIDDLEWARES
from .relay import relay
from .rooms import (
    FALLBACK_USER_AGENT,
    HOP_BY_HOP_HEADERS,
    REQUEST_HOP_BY_HOP_HEADERS,
    _preview,
    redact,
)

logger = logging.getLogger(__name__)

# Deliberately a constant, not a setting, for now. Making it configurable is
# the profile system from the plan, and that is not being built until the
# probe below says the approach works at all - a config field is the one kind
# of change this repo cannot take back cheaply, because Config.model_validate
# silently drops keys it does not know and a later rename resets the user's
# settings without a word.
BLOBCAST_UPSTREAM = "https://blobcast.jackboxgames.com"

# Matched segment-aware, never by bare startswith - the same reasoning as
# rooms.path_is_forwarded: "/room" must not also claim "/roomservice".
BLOBCAST_PREFIXES = ("/room", "/accessToken", "/socket.io", "/blobcast")


def is_blobcast_path(path: str, paths: tuple[str, ...] = BLOBCAST_PREFIXES) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in paths)


# RFC 1035 label shape. Nothing exotic - the point is that "@", "/", ":", "?"
# and whitespace cannot appear, because each of those changes which host a URL
# built from this value resolves to.
_HOSTNAME_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$"
)

# RFC 1035's wire-format ceiling, same one zapret/strategies.py enforces.
MAX_HOSTNAME_LEN = 253


def is_plain_hostname(value: object) -> bool:
    """Whether `value` is a bare hostname safe to interpolate into a URL."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= MAX_HOSTNAME_LEN
        and _HOSTNAME_RE.match(value) is not None
    )


def rewrite_room_response(body: bytes, *, local_host: str) -> tuple[bytes, str | None]:
    """Point the game's socket.io session at this bridge instead of the real
    Blobcast server, and report where it was really headed.

    Blobcast's "server" is a BARE HOSTNAME ("ecast-prod-use2.jackboxgames.com"
    in the captured log), unlike Ecast's, which is a full wss://.../ws URL.
    That difference is what keeps the two rewriters off each other's field: a
    ws:// value here is Ecast's and is left untouched, so nothing this module
    does can reach the Ecast path.

    Returns (possibly-rewritten body, real upstream host or None). Anything
    with nothing to rewrite comes back byte-identical rather than
    re-serialised."""
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body, None

    if not isinstance(parsed, dict):
        return body, None

    server = parsed.get("server")
    if not isinstance(server, str) or not server:
        return body, None
    if server.startswith(("ws://", "wss://")):
        return body, None  # Ecast's field shape - rooms.rewrite_server_field owns it.

    parsed["server"] = local_host
    return json.dumps(parsed).encode("utf-8"), server


class BlobcastSessions:
    """Where the current game's socket.io session really belongs.

    One slot, not a room-code map: this bridge hosts one game at a time, and
    the /room response carries no room code to key on anyway (the code first
    appears later, in the /accessToken request body). A keyed map here would
    be answering a question nothing is asking."""

    def __init__(self) -> None:
        self.upstream: str | None = None
        self.user_agent: str | None = None

    def remember(self, host: str) -> None:
        """Record where the session belongs, if the value is actually a host.

        This string comes off the wire (the upstream's own /room response) and
        is interpolated straight into the URL the socket.io listener then
        connects to. Unvalidated, "ecast.jackboxgames.com@evil.example.com"
        makes that URL resolve to evil.example.com - measured with yarl, not
        reasoned about.

        The asymmetry is what makes this worth guarding: local_server_name,
        which the USER types, is validated strictly in BlobcastSettings, while
        this one arrives from the network and was trusted completely."""
        if not is_plain_hostname(host):
            logger.warning(
                "blobcast: ignoring a server value that is not a bare hostname: %r",
                host[:80],
            )
            return
        if self.upstream != host:
            logger.info("blobcast session upstream is %s", host)
        self.upstream = host

    def remember_user_agent(self, user_agent: str | None) -> None:
        """GET /room carries the game's real User-Agent; the socket.io
        upgrade carries none at all. Capturing it here is the only chance to
        learn it, and the upgrade is refused without one."""
        if user_agent and self.user_agent != user_agent:
            logger.info("blobcast game user-agent is %s", user_agent)
        if user_agent:
            self.user_agent = user_agent


def create_blobcast_handler(
    api_handler, sessions: BlobcastSessions, local_host: str | None
):
    """Wrap the ordinary forwarding handler with the "server" rewrite.

    A wrapper rather than a change inside RoomsProxy: RoomsProxy is on the
    Ecast path too, and this work is not allowed to move that path. Here the
    rewrite can only ever run for a request the router already classified as
    Blobcast.

    `local_host=None` is interception turned off: the field passes through and
    the game takes its session straight to Jackbox. Rooms still get created -
    that was the state before any of this existed - we simply cannot see the
    session. Where it went is still recorded, so turning interception back on
    needs nothing else."""

    async def handle(request: web.Request) -> web.StreamResponse:
        # Captured before anything else: this request has the game's real
        # User-Agent, and the socket.io upgrade later has none at all, so
        # this is the only place to learn it.
        sessions.remember_user_agent(getattr(request, "headers", {}).get("User-Agent"))

        response = await api_handler(request)
        body = getattr(response, "body", None)
        if not isinstance(body, bytes):
            return response  # streamed or empty - nothing to rewrite

        # Probe with a placeholder first: it reports where the session belongs
        # without committing to a rewrite, which is what lets the
        # interception-off path record the upstream and still hand the body
        # back byte-identical.
        new_body, real_server = rewrite_room_response(
            body, local_host=local_host or "<unchanged>"
        )
        if real_server is None:
            return response

        sessions.remember(real_server)
        if not local_host:
            logger.info(
                "blobcast: interception off - server field left as %s, session goes direct",
                real_server,
            )
            return response

        logger.info(
            "blobcast: rewrote server %s -> %s so the socket.io session comes here",
            real_server,
            local_host,
        )
        # Content-Length changes with the body, and aiohttp recomputes it from
        # the new bytes - the stale header would truncate the reply.
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in ("content-length", "content-encoding")
        }
        return web.Response(status=response.status, body=new_body, headers=headers)

    return handle


# Settled by packet capture, not by reading anything: the game connects to
# <hostname from the /room "server" field>:38203 over TLS, SNI set to that
# hostname. The port is in neither /room nor /accessToken (60 and 66 bytes,
# fully accounted for by their known fields) and in no .exe/.dll/.jet/.json
# under the game folder, so it is the game's own constant - it cannot be
# moved from the server side, only listened on.
SOCKETIO_PORT = 38203

# What the "server" field is rewritten to. A hostname, because the game uses
# the value as one and appends SOCKETIO_PORT itself - which is why every
# earlier attempt with a port or a scheme in it failed. "localhost" resolves
# to 127.0.0.1 with no hosts-file edit, and is already a SAN on the bridge's
# existing certificate, so the game's TLS check passes against the CA it
# already trusts.
LOCAL_SERVER_NAME = "localhost"

# An ALLOWLIST, not a blocklist, and that distinction was paid for.
#
# Forwarding "everything except the handshake headers" still got 403.
# Reproduced without the game: a ws_connect carrying ONLY User-Agent gets
# 101, so it is not a missing header but an extra one that the upstream
# refuses. The server names what it accepts in its own response -
# "Access-Control-Allow-Headers: User-Agent,X-Requested-With,
# X-Correlation-Id" - so the allowlist is its list, not a guess.
#
# A blocklist here is the wrong shape anyway: it has to predict every header
# that might offend, and gets it wrong the first time something new appears.
WS_FORWARDED_HEADERS = ("X-Requested-With", "X-Correlation-Id")


def build_socketio_app(
    sessions: BlobcastSessions,
    http_client,
    ws_connector,
    port: int = SOCKETIO_PORT,
    log_frames: bool = False,
) -> web.Application:
    """The listener the game's socket.io session actually lands on.

    Runs on its own port (SOCKETIO_PORT), separate from the bridge's main
    site, because the port is the game's choice and not ours. Frames are
    relayed verbatim in both directions and logged - reading the protocol is
    the entire point, and rewriting anything before it has been read would be
    guessing again."""

    async def handle(request: web.Request) -> web.StreamResponse:
        upstream = sessions.upstream
        if not upstream:
            logger.warning(
                "socket.io %s arrived before any /room told us where the room lives - refusing",
                redact(request.path_qs),  # SECURITY FIX (H3)
            )
            return web.Response(
                status=503,
                body=json.dumps({"ok": False, "error": "no blobcast room known yet"}).encode(),
                content_type="application/json",
            )

        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await _relay_ws(request, upstream)

        url = f"https://{upstream}:{port}{request.path_qs}"
        # SECURITY FIX (H3): path_qs carries the query string, which is where
        # the room token travels - see rooms._QUERY_SECRET_RE.
        logger.info(
            "BLOBCAST http %s %s -> %s", request.method, redact(request.path_qs), redact(url)
        )
        data = await request.read() if request.can_read_body else None
        # Bodies at DEBUG unless log_frames is on, matching the WS side. These
        # were unconditionally at INFO, and /accessToken's body carries the
        # room code - the whole reason log_frames exists is that reading the
        # protocol should be something you turn on, not the default.
        body_level = logging.INFO if log_frames else logging.DEBUG
        if data:
            logger.log(
                body_level,
                "BLOBCAST http request body: %s",
                _preview(data, content_type=request.headers.get("Content-Type")),
            )

        # Connection-scoped headers are stripped here as well as on the Ecast
        # path - aiohttp generates its own, and replaying the game's alongside
        # them is the request-smuggling shape REQUEST_HOP_BY_HOP_HEADERS
        # documents.
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() != "host" and k.lower() not in REQUEST_HOP_BY_HOP_HEADERS
        }
        headers["Host"] = upstream
        result = await http_client.request(request.method, url, headers=headers, data=data)

        logger.log(
            body_level,
            "BLOBCAST http <- %s (%d bytes) %s",
            result.status,
            len(result.body),
            _preview(result.body, content_type=result.headers.get("Content-Type")),
        )
        response_headers = {
            k: v for k, v in result.headers.items() if k.lower() not in HOP_BY_HOP_HEADERS
        }
        return web.Response(status=result.status, body=result.body, headers=response_headers)

    async def _relay_ws(request: web.Request, upstream: str) -> web.StreamResponse:
        url = f"wss://{upstream}:{port}{request.path_qs}"
        # SECURITY FIX (H3): same query string, same token.
        # The session's own URL carries the room in its path, so this follows
        # log_frames too - "off" has to mean the protocol stays out of the log,
        # not just its payloads.
        logger.log(
            logging.INFO if log_frames else logging.DEBUG,
            "BLOBCAST WS upgrade %s -> %s",
            redact(request.path_qs),
            redact(url),
        )

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        # The game's own identity has to survive this hop. Opened without it,
        # Jackbox saw aiohttp's default User-Agent and answered 403, so the
        # room was never created. The handshake headers themselves are
        # dropped: they belong to the game<->bridge negotiation, and aiohttp
        # generates its own for the bridge<->Jackbox one.
        upstream_headers = {
            name: request.headers[name]
            for name in WS_FORWARDED_HEADERS
            if name in request.headers
        }
        # Set, never relayed. The game sends NO User-Agent on this upgrade -
        # measured: the live log showed an empty header set and `101 0 "-" "-"`
        # in the access line. Left unset, aiohttp fills in its own
        # "Python/3.x aiohttp/..." and Jackbox answers 403, while the same
        # upgrade carrying the game's value gets 101.
        #
        # So the value comes from what GET /room recorded, which is the
        # session's real identity, and anything that did arrive on this hop is
        # ignored precisely because it cannot have come from the game.
        upstream_headers["User-Agent"] = sessions.user_agent or FALLBACK_USER_AGENT
        logger.info("BLOBCAST WS upstream headers: %s", upstream_headers)
        try:
            upstream_ws = await ws_connector.connect(url, headers=upstream_headers)
        except Exception as exc:
            logger.error("BLOBCAST WS upstream connect failed: %s", exc)
            await ws.close()
            return ws

        logger.info("BLOBCAST WS connected upstream")
        try:
            await relay(
                ws,
                upstream_ws,
                label_a="blobcast game->server",
                label_b="blobcast server->game",
                # None, not DEBUG. "Debug" reads like off and is not: the Logs
                # screen is handed every record regardless of the configured
                # level, so the old fallback streamed the entire game protocol
                # into the UI with log_frames switched off.
                level=logging.INFO if log_frames else None,
            )
        finally:
            logger.info("BLOBCAST WS closed")
        return ws

    # SECURITY FIX (H1). This site is the second half of the attack the guard
    # closes: a page primed sessions.upstream with a cross-origin GET /room on
    # the main port, then opened a WebSocket here and got a tunnel to Jackbox
    # through the victim's machine. Same middleware as the main app - the port
    # is different, the exposure is identical.
    app = web.Application(middlewares=list(MIDDLEWARES))
    app.router.add_route("*", "/{tail:.*}", handle)
    return app


