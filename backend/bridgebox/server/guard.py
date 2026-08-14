"""Keep web pages out of the bridge.

SECURITY FIX (H1, H2, and M1 along with them). The bridge is an unauthenticated
HTTPS server on 127.0.0.1, and "local" is not the same as "private": every page
the user has open in a browser can reach it. The certificate is no obstacle -
BridgeBox installs its CA machine-wide and localhost is a SAN, so the handshake
succeeds with no warning and no mixed-content block.

What that bought an attacker, all of it default-on:

  H2  Any site could `fetch('https://127.0.0.1:8443/api/v2/rooms', ...)` and
      have it proxied to Jackbox under the user's own address - creating rooms,
      burning rate limits, acting as them.
  H1  A page could prime the Blobcast session with a cross-origin GET /room,
      then open `wss://127.0.0.1:38203/...` and get a two-way tunnel to
      Jackbox's infrastructure through the victim's machine.
  M1  With `server_enabled` on, relay.resolve_room hands any WS connection the
      single known room, so a page could read and inject the live game's frames.

`wants_html` was never a defence against this. It reads `Accept: text/html`,
which correctly catches somebody TYPING the address into a browser - and
fetch/XHR/WebSocket all sail straight past it.

WHY FETCH METADATA, AND NOT Origin
----------------------------------
There is no shared secret to authenticate with: the client is a game we do not
control and cannot give a token to. So the question has to be "did a browser
start this request", and the answer has to come from headers a page cannot
forge. `Sec-Fetch-*` are exactly that - forbidden header names, set by the
browser itself, on every request.

Measured before choosing, over 161 real requests in logs/bridgebox.log.1: not
one carried `Origin` or any `Sec-Fetch-*`. The game is libcurl
("JackboxGames/1.00 libcurl/7.57.0-DEV ... (Win-Steam)") and adds neither.

`Origin` is deliberately NOT a blocking signal, and the asymmetry is the whole
reason: a future Party Pack could plausibly start sending an Origin - it is an
ordinary header any client may set - and blocking on it would break the game
outright for everyone. No client that is not a browser will ever send
`Sec-Fetch-*`. A bare Origin with no fetch metadata is logged instead, so the
field can decide whether tightening is warranted rather than this file guessing.

WHAT THIS DOES NOT FIX
----------------------
A malicious program already running as the user can forge or omit any header it
likes, and this stops none of it. That is not a gap being papered over: such a
program can read the config, the logs and the CA key directly, so the bridge is
not the interesting target. What is closed here is the REMOTE trigger - the
user merely visiting a page.
"""
from __future__ import annotations

import json
import logging

from aiohttp import web

from .rooms import redact

logger = logging.getLogger(__name__)

# Set by the browser, never by page script (forbidden header names), and never
# by a non-browser client. Presence of any one of them means "a browser started
# this", which is the only question that can be answered without a secret.
FETCH_METADATA_HEADERS = ("Sec-Fetch-Site", "Sec-Fetch-Mode", "Sec-Fetch-Dest")

# Response headers that only ever mean something to a browser, stripped on the
# way back. Defence in depth for H2: the bridge replays the upstream's headers,
# so a permissive Access-Control-Allow-Origin from Jackbox would let a page read
# what it managed to send. With the request blocked this is unreachable - which
# is the point of having both.
#
# Set-Cookie is deliberately NOT here. It is not a browser-only header: the game
# may well depend on a cookie round trip, and breaking that to close a hole the
# request block already closes would be a bad trade.
CORS_RESPONSE_HEADERS = frozenset(
    {
        "access-control-allow-origin",
        "access-control-allow-credentials",
        "access-control-allow-methods",
        "access-control-allow-headers",
        "access-control-expose-headers",
        "access-control-max-age",
        "timing-allow-origin",
    }
)


def fetch_metadata(request) -> dict[str, str]:
    """The fetch-metadata headers this request carries, if any."""
    return {
        name: request.headers[name]
        for name in FETCH_METADATA_HEADERS
        if name in request.headers
    }


def is_top_level_navigation(request) -> bool:
    """Somebody typed the address in, or followed a link to it.

    Exempt from the block on purpose, and safe to exempt: a navigation is
    answered by factory.forward_or_warn with a page - the landing page, or the
    red "this is a service address" one - and is NEVER forwarded upstream. That
    behaviour is what makes the exemption free, so it must not be relaxed
    without revisiting this."""
    mode = request.headers.get("Sec-Fetch-Mode", "").lower()
    dest = request.headers.get("Sec-Fetch-Dest", "").lower()
    return mode == "navigate" or dest == "document"


def _refusal(request) -> web.Response:
    body = json.dumps(
        {
            "ok": False,
            "error": (
                "BridgeBox не принимает запросы из браузера. Это локальный мост "
                "для игры; открывать его адрес из веб-страницы не нужно и небезопасно."
            ),
        }
    ).encode("utf-8")
    return web.Response(status=403, body=body, content_type="application/json")


@web.middleware
async def refuse_browser_initiated(request: web.Request, handler):
    """Refuse anything a browser started, unless it is a plain navigation.

    A middleware rather than a check inside each handler, and that is the
    lesson from the token leak fixed in the same audit: that one was not a
    broken function, it was six call sites each having to remember. There are
    three entry points here - the catch-all, the WS relay route, and the whole
    socket.io site on its own port - and a rule that has to be repeated at each
    is a rule that will be missing from the fourth."""
    markers = fetch_metadata(request)
    if markers and not is_top_level_navigation(request):
        logger.warning(
            "refused a browser-initiated request: %s %s (origin=%s, %s)",
            request.method,
            redact(request.path_qs),
            request.headers.get("Origin", "-"),
            ", ".join(f"{k}={v}" for k, v in markers.items()),
        )
        return _refusal(request)

    if not markers and "Origin" in request.headers:
        # Not blocked - see the module docstring on why Origin alone is not a
        # safe signal to act on. Logged so that if this ever shows up from a
        # real browser, the decision can be revisited on evidence.
        logger.info(
            "request carries Origin but no fetch metadata: %s %s (origin=%s)",
            request.method,
            redact(request.path_qs),
            request.headers.get("Origin"),
        )

    response = await handler(request)

    # Already on the wire for a WebSocket - prepare() sent the handshake before
    # the handler returned, and touching the headers then raises.
    if not getattr(response, "prepared", False):
        for name in list(response.headers):
            if name.lower() in CORS_RESPONSE_HEADERS:
                del response.headers[name]
    return response


MIDDLEWARES = (refuse_browser_initiated,)
