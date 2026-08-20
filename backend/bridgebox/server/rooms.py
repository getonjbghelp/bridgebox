from __future__ import annotations

import itertools
import json
import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import aiohttp
from aiohttp import web

if TYPE_CHECKING:
    # Type-only: config.py imports this module for its rewrite defaults, so a
    # runtime import back would be circular.
    from ..config import RewriteConfig

logger = logging.getLogger(__name__)

# How much of a body to put in the log. Ecast payloads carry the game state
# we're reverse-engineering, so the preview has to be long enough to be worth
# reading, and capped so one big frame doesn't bury the rest of the log.
BODY_PREVIEW_CHARS = 800

# Values worth hiding from a log file that may get pasted into a bug report.
# Everything else is shown verbatim - seeing the real headers is the point.
SENSITIVE_HEADERS = {"authorization", "cookie", "set-cookie", "proxy-authorization"}

# The same concern one level down: headers were redacted while bodies went to
# the log verbatim, and a room-creation response carries "token" - the
# credential that controls the room. That log is written to disk unencrypted,
# shown in the Logs screen behind a "Копировать" button, and routinely pasted
# into bug reports. The key stays visible so the line still proves the field
# was present, which is what it is usually being read for.
SENSITIVE_BODY_KEYS = ("token", "authToken", "accessToken", "secret", "password", "userId")

_SECRET_RE = re.compile(
    r'"(' + "|".join(SENSITIVE_BODY_KEYS) + r')"(\s*:\s*)"[^"]*"', re.IGNORECASE
)

# The same secrets one transport over, and this gap was a real leak rather than
# a theoretical one. The room token is not carried in a JSON body when it
# matters most - it is a QUERY PARAMETER, because that is the authorisation
# scheme Jackbox actually accepts (see desktop._close_test_room, where it was
# established by probing the live API). The regex above only ever matched
# "key": "value", so every URL carrying ?token=... went into the log verbatim,
# at INFO, on the request line of every proxied call and every WS upgrade.
#
# That log is written to disk, shown behind a "Копировать" button, and exported
# as .log/.json/.html specifically so it can be pasted into a bug report - so
# the leak ends with the credential in a stranger's hands.
#
# Anchored on "?" or "&" so it cannot match the middle of a word, and the value
# stops at the first character that ends a query parameter. Both the key and
# the separator are preserved: a redacted line still proves the parameter was
# present, which is what these lines are usually read for.
_QUERY_SECRET_RE = re.compile(
    r'(?<=[?&])(' + "|".join(SENSITIVE_BODY_KEYS) + r')(=)[^&\s"\'<>#]*', re.IGNORECASE
)

HIDDEN = "<hidden>"

_request_counter = itertools.count(1)


def redact(text: str) -> str:
    """Blank out credential-ish values in anything on its way to a log.

    Covers both shapes the same secret travels in: a JSON field, and a query
    parameter. One function rather than two, because every call site that
    logs already reaches for this one - a second `redact_url` would be a
    second thing to remember at each new logging line, and forgetting it is
    exactly how the query-string half went unnoticed.

    Regex rather than parse/redact/re-serialise: this runs on previews that
    are truncated mid-document, so the input is frequently not valid JSON by
    the time it arrives - and a redactor that throws on malformed input would
    fail exactly on the payloads worth looking at.

    Safe on any string. It is never used on data going back out on the wire,
    only on text being logged or shown, so over-redacting costs a less
    readable log line and nothing else."""
    text = _SECRET_RE.sub(rf'"\1"\2"{HIDDEN}"', text)
    return _QUERY_SECRET_RE.sub(rf"\1\2{HIDDEN}", text)


# Content types worth rendering as text in the log. Everything else - the
# audio /tts/generate returns, uploaded avatars, voice clips - was decoded
# with errors="replace" into 800 characters of replacement characters per
# request, flooding both the log file and the console. Size and type are the
# only useful facts about a binary body anyway.
TEXTUAL_CONTENT_TYPES = ("json", "text", "xml", "javascript", "x-www-form-urlencoded")


def _is_textual(content_type: str | None) -> bool:
    if not content_type:
        # No type at all: Ecast is JSON throughout, so assume text rather than
        # hiding the payloads this log exists to show.
        return True
    return any(marker in content_type.lower() for marker in TEXTUAL_CONTENT_TYPES)


def _preview(
    body: bytes | None, limit: int = BODY_PREVIEW_CHARS, content_type: str | None = None
) -> str:
    if not body:
        return "<empty>"
    if not _is_textual(content_type):
        return f"<{len(body)} bytes of {content_type}>"
    # Redact the WHOLE body, then truncate - not the other way around.
    # _SECRET_RE needs a closing quote to match; truncating first can cut a
    # real token in half, drop its closing quote out of the slice, and leave
    # the visible half unredacted in the log. See
    # test_a_token_straddling_the_preview_truncation_boundary_leaks_partially.
    text = redact(body.decode("utf-8", errors="replace"))
    suffix = f"... (+{len(body) - limit} bytes)" if len(body) > limit else ""
    return f"{text[:limit]}{suffix}"


def _headers_for_log(headers: dict[str, str]) -> str:
    parts = []
    for key, value in headers.items():
        if key.lower() in SENSITIVE_HEADERS:
            parts.append(f"{key}=<hidden {len(value)} chars>")
        else:
            parts.append(f"{key}={value}")
    return "; ".join(parts)


def _describe_exception(exc: BaseException) -> str:
    # Local copy of diagnostics.describe_exception: that module already
    # imports from this one, so importing it back here would be circular.
    # Bare str(exc) is empty for whole classes of failures (asyncio.
    # TimeoutError being the one that matters for a DPI-blocked upstream).
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__

# Lives here rather than in factory.py so config.py can import every rewrite
# default from one leaf module (this one imports nothing from bridgebox, so
# config -> rooms is a legal direction). factory re-exports it.
UPSTREAM_BASE = "https://ecast.jackboxgames.com"

UPSTREAM_ORIGIN = "https://jackbox.tv"

# Verified empirically against the live API: a request without a browser-like
# User-Agent is rejected by the AWS load balancer with a 403 HTML page before
# it ever reaches the Ecast service, while the same request with one gets a
# real JSON response. Only used as a fallback - the game's own User-Agent is
# forwarded untouched when present.
FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# The research docs pinned the room-code field as "roomid", but they flag that
# example as unverified - and the serverUrl scheme guess from the same source
# already turned out wrong in practice. Accepting the plausible spellings costs
# nothing and stops one naming mismatch from silently breaking WS routing.
ROOM_ID_KEYS = ("roomid", "roomId", "room_id", "room", "code")

# Headers that describe the *upstream* transfer, not the payload we re-serve.
# Replaying them after rewriting the body (which changes its length) would
# truncate or hang the game client.
HOP_BY_HOP_HEADERS = {
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "connection",
    "keep-alive",
}

# The same idea in the other direction, which was missing entirely: RFC 7230
# §6.1 requires a proxy to strip connection-scoped headers on EVERY hop, and
# these were being replayed verbatim to the upstream while aiohttp
# independently generated its own. A request arriving with both Content-Length
# and Transfer-Encoding is the classic request-smuggling shape, and forwarding
# the pair intact is what lets a disagreement between two servers become one.
#
# Content-Encoding is deliberately NOT here, unlike above: the request body is
# forwarded byte-identical, so stripping it would leave the upstream unable to
# decode a body the game legitimately compressed.
REQUEST_HOP_BY_HOP_HEADERS = {
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "upgrade",
    "proxy-connection",
    "te",
    "trailer",
}


@dataclass(frozen=True)
class UpstreamResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class UpstreamClient(Protocol):
    async def request(
        self, method: str, url: str, *, headers: dict[str, str], data: bytes | None
    ) -> UpstreamResponse: ...


# Deliberately NOT a total cap. A total covers the whole exchange including
# reading the body, so it would kill a large transfer that is progressing
# perfectly well. What needs bounding is a connection that has *stopped*:
# sock_read fires when no bytes have arrived for that long, so a slow but
# alive transfer runs as long as it needs while a DPI-blocked one still
# fails fast instead of hanging for aiohttp's five-minute default.
#
# Defensive, not a fix for an observed failure: every request seen in real
# logs so far is small JSON that completes in under a second. The one body
# known to be large is the audio /tts/generate returns.
UPSTREAM_CONNECT_TIMEOUT_S = 10.0
UPSTREAM_READ_STALL_TIMEOUT_S = 20.0


class AiohttpUpstreamClient:
    """Production UpstreamClient backed by a real aiohttp.ClientSession.

    Plain outbound HTTPS to the real Jackbox API - DPI bypass happens
    transparently below this, at the WinDivert/Zapret packet level, not via
    any explicit proxy hop in this client (see PRD "Роль Zapret")."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        timeout: aiohttp.ClientTimeout | None = None,
    ):
        self._session = session
        self._timeout = timeout or aiohttp.ClientTimeout(
            connect=UPSTREAM_CONNECT_TIMEOUT_S,
            sock_read=UPSTREAM_READ_STALL_TIMEOUT_S,
        )

    async def request(
        self, method: str, url: str, *, headers: dict[str, str], data: bytes | None
    ) -> UpstreamResponse:
        async with self._session.request(
            method, url, headers=headers, data=data, timeout=self._timeout
        ) as resp:
            body = await resp.read()
            return UpstreamResponse(status=resp.status, headers=dict(resp.headers), body=body)


def _is_ws_url(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(("ws://", "wss://"))


@dataclass(frozen=True)
class RewriteRules:
    """A RewriteConfig resolved down to what the walk actually needs.

    Resolved once per response, then read-only: the recursion runs per node
    per key, and attribute lookups on a pydantic model in that position are
    the kind of cost that stays invisible until a large game-state frame
    arrives. An empty server_keys tuple is how "disabled" is expressed."""

    local_ws_base: str
    server_keys: tuple[str, ...] = ("server",)
    room_id_keys: tuple[str, ...] = ROOM_ID_KEYS

    @classmethod
    def resolve(cls, local_ws_base: str, rewrite: "RewriteConfig | None") -> "RewriteRules":
        if rewrite is None:
            return cls(local_ws_base=local_ws_base)
        return cls(
            local_ws_base=local_ws_base,
            server_keys=tuple(rewrite.server_keys) if rewrite.server_enabled else (),
            room_id_keys=tuple(rewrite.room_id_keys),
        )


# A bare "host" field (e.g. "ecast-prod-use2.jackboxgames.com", seen on room
# creation) is never rewritten, unlike "server". It was, twice: rewriting it
# to point at this bridge requires the game's own WS connection to succeed
# *through* this bridge's relay - and real traffic (packet capture, not
# guesswork) showed the game never gets a WS handshake through no matter
# what, while the *un*rewritten relay host already works today, because it's
# already on zapret's own hostlist (zapret/lists/list-jackbox.txt) - DPI
# bypass for it happens at the WinDivert/packet level, completely
# independent of this bridge. Rewriting "host" pointed working direct
# traffic at a relay path that doesn't work, for no benefit: nothing about
# the Ecast API traffic itself needed rewriting to begin with. An opt-in
# "server+host" config mode existed for a while to re-test that finding
# without a rebuild; removed after it confirmed the same result again.
#
# `found["server"]` therefore stays empty for a bare "host" (rather than
# reconstructing a wss://<host>/ws value) so callers that gate behavior on
# "was a relay found" - room_relays registration, the old WS-relay
# test_connection step - correctly see none, matching what's actually true:
# this bridge doesn't own that leg of the connection.
def _walk_and_rewrite(node: Any, rules: RewriteRules, found: dict) -> Any:
    """Recursively rewrite relay-pointing fields and pick up the room code
    wherever it sits. Walking the whole document (rather than reading fixed
    top-level keys) means an envelope shape we didn't anticipate - e.g.
    {"ok":true,"body":{...}} - still routes correctly."""
    if isinstance(node, dict):
        result = {}
        for key, value in node.items():
            if key in rules.room_id_keys and isinstance(value, str) and found.get("room_id") is None:
                found["room_id"] = value
            if key in rules.server_keys and _is_ws_url(value):
                if found.get("server") is None:
                    found["server"] = value
                result[key] = rules.local_ws_base
            else:
                result[key] = _walk_and_rewrite(value, rules, found)
        return result
    if isinstance(node, list):
        return [_walk_and_rewrite(item, rules, found) for item in node]
    return node


def rewrite_server_field(
    body: bytes, *, local_ws_base: str, rewrite: "RewriteConfig | None" = None
) -> tuple[bytes, str | None, str | None]:
    """Rewrite a "server" field (full ws(s)://.../ws URL) in an Ecast
    response so the game client connects to this bridge instead of the real
    relay. A bare "host" field is always left untouched - see the comment on
    _walk_and_rewrite for why rewriting it made things worse, not better.

    `rewrite=None` means the built-in defaults, which is what every caller
    that predates RewriteConfig gets.

    Returns (possibly-rewritten body, original relay URL or None, room code
    or None). Non-JSON bodies, and JSON with nothing to rewrite, pass through
    byte-identical - including with rewriting disabled, where the room code
    is still found (test_connection needs it and has no interest in
    rewriting)."""
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body, None, None

    rules = RewriteRules.resolve(local_ws_base, rewrite)
    found: dict = {"server": None, "room_id": None}
    rewritten = _walk_and_rewrite(parsed, rules, found)

    if found["server"] is None:
        # Nothing to redirect - return the original bytes untouched rather
        # than a re-serialized equivalent.
        return body, None, found["room_id"]

    return json.dumps(rewritten).encode("utf-8"), found["server"], found["room_id"]


class RoomsProxy:
    """Forwards Ecast API requests to the real Jackbox API and rewrites the
    response so the game host's next WS connection targets this bridge
    instead of the real relay."""

    def __init__(
        self,
        *,
        upstream_base: str,
        local_ws_base: str,
        http_client: UpstreamClient,
        room_relays: dict[str, str],
        rewrite: "RewriteConfig | None" = None,
    ):
        self._upstream_base = upstream_base.rstrip("/")
        self._upstream_host = upstream_base.split("//", 1)[-1].split("/", 1)[0]
        self._local_ws_base = local_ws_base
        self._http_client = http_client
        self._room_relays = room_relays
        self._rewrite = rewrite
        # None means "this module is off" - the header is then left exactly as
        # the game sent it, rather than substituted. Callers predating
        # RewriteConfig (rewrite=None) keep both modules on.
        if rewrite is None:
            self._origin: str | None = UPSTREAM_ORIGIN
            self._fallback_user_agent: str | None = FALLBACK_USER_AGENT
        else:
            self._origin = rewrite.upstream_origin if rewrite.origin_enabled else None
            self._fallback_user_agent = (
                rewrite.fallback_user_agent if rewrite.user_agent_enabled else None
            )

    async def forward(
        self, method: str, path: str, *, headers: dict[str, str], data: bytes | None
    ) -> UpstreamResponse:
        # Host always goes - the upstream vhost depends on it, and the
        # incoming one names this bridge. Origin is only stripped when we
        # have a replacement; with that module off the game's own Origin
        # travels through untouched instead of being silently dropped.
        dropped = REQUEST_HOP_BY_HOP_HEADERS | ({"host", "origin"} if self._origin else {"host"})
        upstream_headers = {
            key: value for key, value in headers.items() if key.lower() not in dropped
        }
        upstream_headers["Host"] = self._upstream_host
        if self._origin:
            upstream_headers["Origin"] = self._origin
        if self._fallback_user_agent and not any(
            key.lower() == "user-agent" for key in upstream_headers
        ):
            upstream_headers["User-Agent"] = self._fallback_user_agent

        url = f"{self._upstream_base}{path}"
        # A short id ties each "->" line to its "<-" line; without it
        # concurrent game requests interleave into an unreadable log.
        rid = f"req{next(_request_counter):04d}"
        # SECURITY FIX (H3): `path` is path_qs - it carries the query string,
        # and the room token lives there. Redacted once here and reused, so a
        # future log line added to this method cannot reintroduce the leak by
        # reaching for the raw variable.
        safe_path = redact(path)
        safe_url = redact(url)

        logger.info("[%s] -> %s %s (%d bytes)", rid, method, safe_path, len(data or b""))
        logger.debug("[%s] request headers: %s", rid, _headers_for_log(upstream_headers))
        if data:
            request_type = next(
                (v for k, v in upstream_headers.items() if k.lower() == "content-type"), None
            )
            logger.debug("[%s] request body: %s", rid, _preview(data, content_type=request_type))

        started = time.monotonic()
        try:
            upstream_response = await self._http_client.request(
                method, url, headers=upstream_headers, data=data
            )
        except Exception as exc:
            # Most likely the DPI block itself (upstream unreachable) - this is
            # the single most useful line in the log when nothing works.
            logger.error(
                "[%s] upstream request failed after %.0fms for %s: %s: %s",
                rid,
                (time.monotonic() - started) * 1000,
                safe_url,  # SECURITY FIX (H3): carries the query string
                type(exc).__name__,
                exc,
            )
            raise

        elapsed_ms = (time.monotonic() - started) * 1000
        logger.info(
            "[%s] <- HTTP %s in %.0fms (%d bytes, %s)",
            rid,
            upstream_response.status,
            elapsed_ms,
            len(upstream_response.body),
            upstream_response.headers.get("Content-Type", "?"),
        )
        response_type = upstream_response.headers.get("Content-Type")
        logger.debug("[%s] response headers: %s", rid, _headers_for_log(upstream_response.headers))
        logger.debug(
            "[%s] response body: %s",
            rid,
            _preview(upstream_response.body, content_type=response_type),
        )
        if upstream_response.status >= 400:
            logger.warning(
                "[%s] upstream error %s for %s: %s",
                rid,
                upstream_response.status,
                safe_path,  # SECURITY FIX (H3)
                _preview(upstream_response.body, 400, content_type=response_type),
            )

        new_body, original_server, room_id = rewrite_server_field(
            upstream_response.body,
            local_ws_base=self._local_ws_base,
            rewrite=self._rewrite,
        )
        if original_server:
            key = room_id or "*"
            self._room_relays[key] = original_server
            logger.info(
                "[%s] room %s: rewrote server %s -> %s", rid, key, original_server, self._local_ws_base
            )
            logger.debug("[%s] rewritten body: %s", rid, _preview(new_body))
        else:
            # Not an error - most endpoints have no "server" field - but when
            # the game fails to reach the relay this line says whether the
            # rewrite simply never fired.
            logger.debug(
                "[%s] no relay field (\"server\" or \"host\") in response, body passed through",
                rid,
            )

        return UpstreamResponse(
            status=upstream_response.status,
            headers=upstream_response.headers,
            body=new_body,
        )


def path_is_forwarded(path: str, prefixes) -> bool:
    """Whether `path` sits under one of `prefixes`.

    Segment-aware on purpose: a bare startswith() would let the prefix "/api"
    also claim "/apifoo", which is a different endpoint entirely. A prefix of
    "/" matches everything, which is the same thing forward_all does."""
    for prefix in prefixes:
        if prefix == "/" or path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def create_api_handler(proxy: RoomsProxy):
    """Build the handler that forwards one request upstream and re-serves the
    reply. Exposed separately from register_api_proxy because factory.py also
    uses it as the fallback for paths outside /api - see build_full_app."""

    async def handle(request: web.Request) -> web.Response:
        data = await request.read() if request.can_read_body else None
        # The game's own request, before any header substitution - this is
        # what a real Party Pack client actually sends.
        logger.debug(
            "game request: %s %s from %s | %s",
            request.method,
            # SECURITY FIX (H3): path_qs carries the query string, and the
            # room token is a query parameter.
            redact(request.path_qs),
            request.remote,
            _headers_for_log(dict(request.headers)),
        )
        try:
            result = await proxy.forward(
                request.method, request.path_qs, headers=dict(request.headers), data=data
            )
        except Exception as exc:
            # Most likely the DPI block this whole bridge exists to route
            # around - an expected operational condition (zapret strategy
            # not currently working, or genuinely offline), not a bug.
            # Without this, aiohttp's default unhandled-exception path
            # returned a bare 500 with a plain-text body (every other error
            # in this proxy is JSON, which the game can at least attempt to
            # parse) and logged a full traceback as if this were a crash.
            # forward() already logged the detailed upstream failure.
            body = json.dumps(
                {"ok": False, "error": f"upstream unreachable: {_describe_exception(exc)}"}
            ).encode("utf-8")
            return web.Response(status=502, body=body, content_type="application/json")

        headers = {
            key: value
            for key, value in result.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
        }
        if not any(key.lower() == "content-type" for key in headers):
            # Ecast is a JSON API throughout; without this aiohttp would label
            # the body application/octet-stream and a strict client could
            # refuse to parse it.
            headers["Content-Type"] = "application/json"
        return web.Response(status=result.status, body=result.body, headers=headers)

    return handle


def register_api_proxy(app: web.Application, proxy: RoomsProxy, *, prefix: str = "/api") -> None:
    """Proxy every method and every path under `prefix` to the real API.

    Deliberately not a per-endpoint route table: the PRD's whole premise is a
    bridge that doesn't need updating for each new Party Pack, and any
    endpoint we failed to anticipate would otherwise fall through to the
    browser-warning page and hand the game an HTML page where it expected
    JSON."""
    app.router.add_route("*", f"{prefix}/{{tail:.*}}", create_api_handler(proxy))
