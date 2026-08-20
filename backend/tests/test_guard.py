"""Web pages must not be able to use the bridge (H1, H2, M1).

The bridge authenticates nothing and listens on 127.0.0.1, which every page in
the user's browser can reach - and reach without a certificate warning, because
BridgeBox installs its CA machine-wide and localhost is a SAN.

These tests drive real aiohttp apps through a real client, with the headers a
real browser sends, because the whole question is what the SERVER does with a
header set it does not control.

The header choice is measured, not assumed: over 161 real requests in
logs/bridgebox.log.1 the game sent no Origin and no Sec-Fetch-* - it is libcurl
("JackboxGames/1.00 libcurl/7.57.0-DEV ... (Win-Steam)"). See guard.py.
"""
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bridgebox.server import guard
from bridgebox.server.blobcast import BlobcastSessions, build_socketio_app
from bridgebox.server.factory import build_full_app
from bridgebox.server.rooms import UpstreamResponse

# What Chrome sends for `fetch()` from another site. Sec-Fetch-* are forbidden
# header names - page script cannot set or remove them.
BROWSER_FETCH = {
    "Origin": "https://evil.example.com",
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Accept": "*/*",
}

# What it sends for `new WebSocket('wss://127.0.0.1:8443/...')`. The handshake
# headers are here so the request actually reaches the relay route - both relay
# handlers gate on `Upgrade`, and without it this would exercise the ordinary
# HTTP path instead and prove nothing about the WS one.
BROWSER_WS = {
    "Origin": "https://evil.example.com",
    "Sec-Fetch-Site": "cross-site",
    "Sec-Fetch-Mode": "websocket",
    "Sec-Fetch-Dest": "websocket",
    "Upgrade": "websocket",
    "Connection": "Upgrade",
    "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
    "Sec-WebSocket-Version": "13",
}

# Somebody typing the address into the URL bar. Must keep working.
BROWSER_NAVIGATION = {
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# The real thing, copied from the log.
GAME = {
    "User-Agent": "JackboxGames/1.00 libcurl/7.57.0-DEV OpenSSL/1.1.1g zlib/1.2.7 (Win-Steam)",
    "Accept": "*/*",
}


class FakeUpstream:
    """Records whether anything actually reached Jackbox."""

    def __init__(self, body: bytes = b'{"ok":true}', headers: dict | None = None):
        self.calls: list[tuple[str, str]] = []
        self._body = body
        self._headers = headers or {"Content-Type": "application/json"}

    async def request(self, method, url, *, headers, data):
        self.calls.append((method, url))
        return UpstreamResponse(status=200, headers=dict(self._headers), body=self._body)


class FakeWsConnector:
    def __init__(self):
        self.connected_url = None

    async def connect(self, url, *, headers=None):
        self.connected_url = url
        raise AssertionError("the guard should have refused before we got here")


async def _client(app) -> TestClient:
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


# ---- H2: the HTTP proxy -------------------------------------------------


async def test_a_page_cannot_proxy_a_request_to_jackbox():
    """The H2 finding. A site could POST through the bridge and have it
    forwarded under the user's own address - creating rooms, burning rate
    limits, acting as them."""
    upstream = FakeUpstream()
    app = build_full_app(
        host="127.0.0.1", port=8443, http_client=upstream, ws_connector=FakeWsConnector()
    )
    client = await _client(app)
    try:
        response = await client.post(
            "/api/v2/rooms", json={"apptag": "fourbage"}, headers=BROWSER_FETCH
        )
        body = await response.json()
    finally:
        await client.close()

    assert response.status == 403
    assert upstream.calls == [], "the request reached Jackbox"
    assert body["ok"] is False


async def test_h2_stays_closed_for_an_object_or_embed_style_navigation():
    """SECURITY FIX for a gap between two different tests the code used to run.

    guard.is_top_level_navigation exempts a request from the block if EITHER
    Sec-Fetch-Mode == "navigate" OR Sec-Fetch-Dest == "document" - and per the
    Fetch Metadata spec, an `<object data=...>` or `<embed src=...>` load is a
    real, browser-set Sec-Fetch-Mode: navigate with Sec-Fetch-Dest: object (or
    embed), NOT "document". That alone is enough to pass the guard.

    factory.forward_or_warn used to decide whether to answer with a page
    (never forwarding) using a DIFFERENT test: wants_html(), which only
    looked at Accept containing "text/html". An object/embed load's Accept
    header is not required to contain text/html - browsers do not always
    send it there, since the destination can be arbitrary content, not
    necessarily HTML. So a request could satisfy "is a navigation" (passing
    the guard) while failing "wants html" (skipping the page-only branch) at
    the same time, and fall straight through into api_handler - upstream,
    exactly like the plain H2 case this file already pins.

    Fixed by making forward_or_warn's page branch fire on
    is_top_level_navigation() too, not only wants_html() - the same
    predicate the guard itself exempts on, so nothing the guard lets through
    as "a navigation" can ever reach a forwarding branch. Headers here match
    the Fetch Metadata spec's per-destination values for an object/embed
    load; whether Chrome's actual Accept header for every such load excludes
    text/html in every version is a separate question from the code-level
    gap this test pins, which is closed regardless."""
    upstream = FakeUpstream()
    app = build_full_app(
        host="127.0.0.1", port=8443, http_client=upstream, ws_connector=FakeWsConnector()
    )
    client = await _client(app)
    try:
        response = await client.post(
            "/api/v2/rooms",
            json={"apptag": "fourbage"},
            headers={
                "Origin": "https://evil.example.com",
                "Sec-Fetch-Site": "cross-site",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "embed",
                "Accept": "*/*",
            },
        )
    finally:
        await client.close()

    assert upstream.calls == [], "an embed/object-shaped navigation must never reach Jackbox"
    assert response.status == 403


async def test_the_game_is_not_affected():
    """The failure that would matter most. The game is libcurl and sends no
    fetch metadata, so it must pass through untouched."""
    upstream = FakeUpstream()
    app = build_full_app(
        host="127.0.0.1", port=8443, http_client=upstream, ws_connector=FakeWsConnector()
    )
    client = await _client(app)
    try:
        response = await client.post("/api/v2/rooms", json={"apptag": "fourbage"}, headers=GAME)
    finally:
        await client.close()

    assert response.status == 200
    assert len(upstream.calls) == 1, "the game's request was not forwarded"


async def test_typing_the_address_into_a_browser_still_shows_the_page():
    """The landing page is a deliberate feature and the exemption that allows
    it is safe: forward_or_warn answers a navigation with a page and never
    forwards it upstream."""
    upstream = FakeUpstream()
    app = build_full_app(
        host="127.0.0.1", port=8443, http_client=upstream, ws_connector=FakeWsConnector()
    )
    client = await _client(app)
    try:
        response = await client.get("/", headers=BROWSER_NAVIGATION)
        text = await response.text()
    finally:
        await client.close()

    assert response.status == 200
    assert "BridgeBox" in text
    assert upstream.calls == [], "a navigation must never reach Jackbox"


async def test_navigating_to_a_service_path_still_gets_the_red_page():
    """The other half of the existing behaviour: a browser on /api is told off
    rather than proxied, and that page is what says why."""
    upstream = FakeUpstream()
    app = build_full_app(
        host="127.0.0.1", port=8443, http_client=upstream, ws_connector=FakeWsConnector()
    )
    client = await _client(app)
    try:
        response = await client.get("/api/v2/rooms/ABCD", headers=BROWSER_NAVIGATION)
        text = await response.text()
    finally:
        await client.close()

    assert response.status == 403
    assert "служебный" in text
    assert upstream.calls == []


async def test_a_preflight_is_refused_too():
    """Blocking the preflight is what stops the real request from ever being
    attempted."""
    upstream = FakeUpstream()
    app = build_full_app(
        host="127.0.0.1", port=8443, http_client=upstream, ws_connector=FakeWsConnector()
    )
    client = await _client(app)
    try:
        response = await client.options(
            "/api/v2/rooms",
            headers={**BROWSER_FETCH, "Sec-Fetch-Mode": "cors",
                     "Access-Control-Request-Method": "POST"},
        )
    finally:
        await client.close()

    assert response.status == 403
    assert upstream.calls == []


async def test_permissive_cors_from_upstream_is_not_replayed():
    """Defence in depth for H2: the bridge replays the upstream's headers, so
    a permissive Access-Control-Allow-Origin from Jackbox would let a page read
    whatever it managed to send. Unreachable once the request is blocked -
    which is exactly why both exist."""
    upstream = FakeUpstream(
        headers={
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Credentials": "true",
        }
    )
    app = build_full_app(
        host="127.0.0.1", port=8443, http_client=upstream, ws_connector=FakeWsConnector()
    )
    client = await _client(app)
    try:
        response = await client.get("/api/v2/rooms/ABCD", headers=GAME)
    finally:
        await client.close()

    assert response.status == 200
    assert "Access-Control-Allow-Origin" not in response.headers
    assert "Access-Control-Allow-Credentials" not in response.headers


async def test_set_cookie_is_left_alone():
    """Deliberately not stripped: it is not a browser-only header, the game may
    depend on a cookie round trip, and the request block already closes the
    hole that stripping it would address."""
    upstream = FakeUpstream(
        headers={"Content-Type": "application/json", "Set-Cookie": "session=abc; Path=/"}
    )
    app = build_full_app(
        host="127.0.0.1", port=8443, http_client=upstream, ws_connector=FakeWsConnector()
    )
    client = await _client(app)
    try:
        response = await client.get("/api/v2/rooms/ABCD", headers=GAME)
    finally:
        await client.close()

    assert "Set-Cookie" in response.headers


# ---- H1 / M1: the WebSocket relays --------------------------------------


async def test_a_page_cannot_open_the_ecast_relay():
    """The M1 finding, which shares H1's fix: relay.resolve_room hands any WS
    connection the single known room, so a page could read and inject the live
    game's frames."""
    connector = FakeWsConnector()
    app = build_full_app(
        host="127.0.0.1", port=8443, http_client=FakeUpstream(), ws_connector=connector
    )
    client = await _client(app)
    try:
        response = await client.get("/ws?roomid=ABCD", headers=BROWSER_WS)
    finally:
        await client.close()

    assert response.status == 403
    assert connector.connected_url is None


async def test_a_page_cannot_open_the_blobcast_tunnel():
    """The H1 finding end to end. sessions.upstream is already primed - the
    page did that with a cross-origin GET /room - and this is the second half:
    a WebSocket to the socket.io port that gets relayed to Jackbox through the
    victim's machine."""
    sessions = BlobcastSessions()
    sessions.remember("ecast-prod-use2.jackboxgames.com")
    connector = FakeWsConnector()
    app = build_socketio_app(sessions, FakeUpstream(), connector, port=38203)
    client = await _client(app)
    try:
        response = await client.get("/socket.io/?EIO=3&transport=websocket", headers=BROWSER_WS)
    finally:
        await client.close()

    assert response.status == 403
    assert connector.connected_url is None, "a tunnel to Jackbox was opened"


async def test_a_page_navigation_to_the_socketio_port_is_refused_not_forwarded():
    """SECURITY FIX for a break in the invariant guard.is_top_level_navigation's
    docstring states as the whole justification for exempting navigations
    from the block:

        "a navigation is answered by factory.forward_or_warn with a page ...
        and is NEVER forwarded upstream. That behaviour is what makes the
        exemption free, so it must not be relaxed without revisiting this."

    That promise is kept by factory.forward_or_warn (main app), which checks
    wants_html() and answers with a page before anything could be forwarded.
    build_socketio_app has no page to serve at all - its `handle` forwards
    any non-WS request straight to the real Jackbox upstream - so it used to
    reuse the main app's permissive middleware anyway and let an exempted
    navigation (`window.location = ...`, a clicked link, or an
    auto-submitting <form> - no user gesture required) reach Jackbox under
    the game's identity, attacker-chosen path and query string included
    (exactly where the room's accessToken travels).

    Fixed by building this site's middleware with
    guard.build_guard_middlewares(exempt_navigation=False): a browser-shaped
    request is refused outright here, navigation or not, because this port
    has no safe way to answer one with anything but a refusal. Severity of
    the original gap: remote / triggerable by any page open in the user's
    browser while a Party Pack 1-6 game is active; no user interaction beyond
    having the page loaded."""
    sessions = BlobcastSessions()
    sessions.remember("ecast-prod-use2.jackboxgames.com")
    upstream = FakeUpstream(body=b"<html>whatever jackbox answered</html>")
    app = build_socketio_app(sessions, upstream, FakeWsConnector(), port=38203)
    client = await _client(app)
    try:
        response = await client.get(
            "/accessToken?code=ABCD&secret=whatever", headers=BROWSER_NAVIGATION
        )
    finally:
        await client.close()

    assert upstream.calls == [], "a browser navigation must never reach Jackbox - see the guard module docstring"
    assert response.status == 403


async def test_a_page_cannot_prime_the_blobcast_session():
    """The first half of the same chain: without this, a page could point the
    socket.io listener at any host the upstream names, just by making the
    bridge fetch /room for it."""
    upstream = FakeUpstream(body=json.dumps({"server": "evil.example.com"}).encode())
    app = build_full_app(
        host="127.0.0.1", port=8443, http_client=upstream, ws_connector=FakeWsConnector()
    )
    client = await _client(app)
    try:
        response = await client.get("/room", headers=BROWSER_FETCH)
    finally:
        await client.close()

    assert response.status == 403
    assert upstream.calls == []


async def test_the_socketio_listener_still_serves_the_game():
    """The blobcast session is the reason that port exists; blocking the game
    there would break Party Packs 1-6 outright."""
    sessions = BlobcastSessions()
    sessions.remember("ecast-prod-use2.jackboxgames.com")
    upstream = FakeUpstream()
    app = build_socketio_app(sessions, upstream, FakeWsConnector(), port=38203)
    client = await _client(app)
    try:
        response = await client.get("/socket.io/?EIO=3&transport=polling", headers=GAME)
    finally:
        await client.close()

    assert response.status == 200
    assert len(upstream.calls) == 1


# ---- the rule itself ----------------------------------------------------


def test_origin_alone_is_not_treated_as_proof():
    """Deliberate, and the asymmetry is the reason: a future Party Pack could
    plausibly start sending an Origin - it is an ordinary header any client may
    set - and blocking on it would break the game for everyone. No non-browser
    client will ever send Sec-Fetch-*."""

    class Req:
        headers = {"Origin": "https://jackbox.tv"}

    assert guard.fetch_metadata(Req()) == {}


def test_a_navigation_is_recognised_by_either_signal():
    class Mode:
        headers = {"Sec-Fetch-Mode": "navigate"}

    class Dest:
        headers = {"Sec-Fetch-Dest": "document"}

    class Cors:
        headers = {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}

    assert guard.is_top_level_navigation(Mode()) is True
    assert guard.is_top_level_navigation(Dest()) is True
    assert guard.is_top_level_navigation(Cors()) is False


@pytest.mark.parametrize(
    "builder",
    [
        lambda: build_full_app(
            host="127.0.0.1", port=8443, http_client=FakeUpstream(),
            ws_connector=FakeWsConnector(),
        ),
        lambda: build_socketio_app(BlobcastSessions(), FakeUpstream(), FakeWsConnector()),
    ],
    ids=["main app", "socket.io app"],
)
def test_every_app_this_project_serves_carries_the_guard(builder):
    """The structural half. Both sites are reachable from a browser, and a new
    one added later without the middleware would reopen the hole silently -
    exactly the "one more call site forgot" shape the token leak had.

    Checked by name, not identity: build_guard_middlewares is a factory now
    (see its docstring - the socket.io site needs a stricter,
    exempt_navigation=False instance), so each app's middleware is its own
    closure rather than one shared function object."""
    app = builder()

    assert any(getattr(mw, "__name__", "") == "refuse_browser_initiated" for mw in app.middlewares)
