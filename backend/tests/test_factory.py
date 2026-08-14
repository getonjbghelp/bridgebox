import json

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from bridgebox.server.factory import build_full_app
from bridgebox.server.rooms import UpstreamResponse

ROOM_INFO_JSON = json.dumps(
    {
        "apptag": "fibbage3",
        "roomid": "ABCD",
        "server": "wss://ecast-relay-prod-01.jackboxgames.com/ws",
    }
).encode("utf-8")


class FakeUpstreamClient:
    def __init__(self, response: UpstreamResponse):
        self._response = response

    async def request(self, method, url, *, headers, data):
        return self._response


class FakeWS:
    def __init__(self):
        import asyncio

        self._inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []
        self.closed = False

    async def feed_text(self, data: str) -> None:
        self._inbox.put_nowait(aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, data, None))

    async def send_str(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True
        self._inbox.put_nowait(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item


class FakeWsConnector:
    def __init__(self, ws: FakeWS):
        self._ws = ws
        self.connected_url = None

    async def connect(self, url: str):
        self.connected_url = url
        return self._ws


async def test_build_full_app_wires_rooms_and_relay_through_shared_room_relays():
    http_client = FakeUpstreamClient(
        UpstreamResponse(status=200, headers={}, body=ROOM_INFO_JSON)
    )
    upstream_ws = FakeWS()
    await upstream_ws.feed_text('{"opcode":"welcome"}')
    ws_connector = FakeWsConnector(upstream_ws)

    app = build_full_app(
        host="127.0.0.1", port=8443, http_client=http_client, ws_connector=ws_connector
    )

    async with TestClient(TestServer(app)) as client:
        # 1. create the room - the local bridge should rewrite "server" to itself
        create_resp = await client.post("/api/v2/rooms", data=b"{}")
        assert create_resp.status == 200
        create_body = await create_resp.json()
        assert create_body["server"] == "wss://127.0.0.1:8443/ws"

        # 2. the game host then opens the WS the rewritten "server" pointed at -
        # relay.py must be able to find ABCD's real relay via the room_relays
        # map that rooms.py populated, without any extra wiring by the caller.
        ws = await client.ws_connect("/ws?role=host&roomId=ABCD&token=xyz")
        msg = await ws.receive()
        assert msg.type == aiohttp.WSMsgType.TEXT
        assert msg.data == '{"opcode":"welcome"}'
        await ws.close()

    assert ws_connector.connected_url == (
        "wss://ecast-relay-prod-01.jackboxgames.com/ws?role=host&roomId=ABCD&token=xyz"
    )


async def test_a_non_api_game_path_is_forwarded_upstream_not_answered_with_html():
    """FixyText (RiskyText in the game files) POSTs its TTS job to
    /tts/generate, which is not under /api/. It fell through to the browser
    warning page, so the game got HTTP 200 with an HTML document where it
    expected its TTS result - captured live:

        unmatched request: POST /tts/generate
        "POST /tts/generate HTTP/1.1" 200 621

    The game is pointed at this bridge as its entire server, so a path we
    don't recognise is not an error - it's an endpoint we hadn't seen, and
    the real server is the one that knows what to do with it."""
    recorded: list[tuple[str, str]] = []

    class RecordingUpstream:
        async def request(self, method, url, *, headers, data):
            recorded.append((method, url))
            return UpstreamResponse(
                status=200, headers={"Content-Type": "audio/mpeg"}, body=b"ID3\x04audio"
            )

    app = build_full_app(
        host="127.0.0.1",
        port=8443,
        http_client=RecordingUpstream(),
        ws_connector=FakeWsConnector(FakeWS()),
    )

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/tts/generate", data=b'{"text":"hello"}')

        assert resp.status == 200
        assert await resp.read() == b"ID3\x04audio"  # not the HTML stub
        assert resp.headers["Content-Type"] == "audio/mpeg"

    assert recorded == [("POST", "https://ecast.jackboxgames.com/tts/generate")]


async def test_a_browser_visiting_the_bridge_still_gets_the_warning_page():
    """The stub is for a human who typed the address in, so it is gated on
    the caller actually being able to render HTML - the game's libcurl sends
    Accept: */* and must never be handed a web page."""
    app = build_full_app(
        host="127.0.0.1",
        port=8443,
        http_client=FakeUpstreamClient(UpstreamResponse(200, {}, ROOM_INFO_JSON)),
        ws_connector=FakeWsConnector(FakeWS()),
    )

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/", headers={"Accept": "text/html,application/xhtml+xml"})

        assert resp.status == 200
        assert "BridgeBox" in await resp.text()


async def test_the_browser_pages_language_follows_a_live_setting_not_a_snapshot():
    """lang= is a callable, not a resolved string, precisely so a language
    switch in Settings (which promises "no restart needed") reaches these
    pages without a bridge restart. A regression back to a plain string
    parameter would still pass every other test in this file - only reading
    the SAME app object twice, with the callable's answer changed in
    between, catches it."""
    preference = {"lang": "ru"}
    app = build_full_app(
        host="127.0.0.1",
        port=8443,
        http_client=FakeUpstreamClient(UpstreamResponse(200, {}, ROOM_INFO_JSON)),
        ws_connector=FakeWsConnector(FakeWS()),
        lang=lambda: preference["lang"],
    )

    async with TestClient(TestServer(app)) as client:
        # /api is a well-known Ecast service path, so wants_html + this path
        # together take the service_page() branch rather than the landing one.
        ru_resp = await client.get("/api", headers={"Accept": "text/html"})
        ru_text = await ru_resp.text()

        preference["lang"] = "en"
        en_resp = await client.get("/api", headers={"Accept": "text/html"})
        en_text = await en_resp.text()

    assert "Так делать не нужно" in ru_text
    assert "This isn't meant to be opened" in en_text


async def test_a_large_binary_asset_passes_through_byte_identical():
    """The log preview is capped at BODY_PREVIEW_CHARS and prints
    "... (+N bytes)", which reads like the bridge truncates what it forwards.
    It does not - only the log line is shortened. The body known to be large
    on this path is the audio /tts/generate returns; this pins the guarantee
    for any of them."""
    asset = bytes(range(256)) * 4096  # 1 MiB of non-UTF-8 bytes

    class BigUpstream:
        async def request(self, method, url, *, headers, data):
            return UpstreamResponse(
                status=200, headers={"Content-Type": "audio/mpeg"}, body=asset
            )

    app = build_full_app(
        host="127.0.0.1",
        port=8443,
        http_client=BigUpstream(),
        ws_connector=FakeWsConnector(FakeWS()),
    )

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/tts/generate/result.mp3")
        received = await resp.read()

    assert len(received) == len(asset)
    assert received == asset


async def test_a_large_request_body_reaches_upstream_intact():
    """The other direction: an avatar or voice upload POSTed by a controller."""
    payload = bytes(range(256)) * 2048  # 512 KiB
    seen: list[bytes] = []

    class RecordingUpstream:
        async def request(self, method, url, *, headers, data):
            seen.append(data)
            return UpstreamResponse(status=200, headers={}, body=b"{}")

    app = build_full_app(
        host="127.0.0.1",
        port=8443,
        http_client=RecordingUpstream(),
        ws_connector=FakeWsConnector(FakeWS()),
    )

    async with TestClient(TestServer(app)) as client:
        await client.post("/media/upload", data=payload)

    assert seen[0] == payload


# ---- configurable proxy paths -------------------------------------------


def _app_with_proxy(proxy_config, upstream=None):
    from bridgebox.server.rooms import UpstreamResponse

    class Ok:
        async def request(self, method, url, *, headers, data):
            return UpstreamResponse(
                status=200, headers={"Content-Type": "application/json"}, body=b'{"ok":true}'
            )

    return build_full_app(
        host="127.0.0.1",
        port=8443,
        http_client=upstream or Ok(),
        ws_connector=FakeWsConnector(FakeWS()),
        proxy_config=proxy_config,
    )


async def test_forward_all_on_proxies_a_path_outside_the_list():
    """The shipped default. A path nobody listed is an endpoint nobody had
    seen yet, not an error - that is exactly how /tts/generate broke."""
    from bridgebox.config import ProxyConfig

    app = _app_with_proxy(ProxyConfig(forward_all=True, paths=["/api"]))

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/something/nobody/listed")
        assert resp.status == 200
        assert (await resp.json())["ok"] is True


async def test_forward_all_off_refuses_a_path_outside_the_list():
    from bridgebox.config import ProxyConfig

    app = _app_with_proxy(ProxyConfig(forward_all=False, paths=["/api", "/tts"]))

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/media/upload")

        # JSON, not the browser stub with HTTP 200 - the silent-HTML failure
        # is the bug this whole path exists because of.
        assert resp.status == 404
        assert (await resp.json())["ok"] is False
        assert "/media/upload" in (await resp.json())["error"]


async def test_forward_all_off_still_proxies_the_listed_paths():
    from bridgebox.config import ProxyConfig

    app = _app_with_proxy(ProxyConfig(forward_all=False, paths=["/api", "/tts"]))

    async with TestClient(TestServer(app)) as client:
        for path in ("/api/v2/rooms", "/tts/generate", "/api", "/tts"):
            resp = await client.post(path)
            assert resp.status == 200, path


async def test_a_prefix_does_not_claim_a_longer_sibling_segment():
    """"/api" must not also capture "/apifoo" - a different endpoint."""
    from bridgebox.config import ProxyConfig

    app = _app_with_proxy(ProxyConfig(forward_all=False, paths=["/api"]))

    async with TestClient(TestServer(app)) as client:
        assert (await client.post("/apifoo/bar")).status == 404
        assert (await client.post("/api/bar")).status == 200


async def test_removing_api_from_the_list_actually_stops_proxying_it():
    """There used to be a hardcoded /api route registered ahead of the
    catch-all. With it, editing this setting would have silently done
    nothing for the one prefix users are most likely to touch."""
    from bridgebox.config import ProxyConfig

    app = _app_with_proxy(ProxyConfig(forward_all=False, paths=["/tts"]))

    async with TestClient(TestServer(app)) as client:
        assert (await client.post("/api/v2/rooms")).status == 404


async def test_a_browser_still_gets_the_warning_page_even_when_the_path_is_refused():
    from bridgebox.config import ProxyConfig

    app = _app_with_proxy(ProxyConfig(forward_all=False, paths=["/api"]))

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/", headers={"Accept": "text/html"})
        assert "BridgeBox" in await resp.text()


# ---- Blobcast (Party Pack 1-6) alongside Ecast ----------------------------
#
# The two never collide: Ecast is /api/v2/*, Blobcast is /room, /accessToken
# and /socket.io/*. These tests pin that separation, because the whole reason
# one listener can serve both without a mode switch is that the paths are
# disjoint - if that ever stops being true, it should fail here and not in
# somebody's game.


class RecordingUpstream:
    """Records where each request was sent, and answers with a canned body."""

    def __init__(self, body=b'{"ok":true}', content_type="application/json"):
        self.calls: list[tuple[str, str]] = []
        self._body = body
        self._content_type = content_type

    async def request(self, method, url, *, headers, data):
        self.calls.append((method, url))
        return UpstreamResponse(
            status=200, headers={"Content-Type": self._content_type}, body=self._body
        )


def _blobcast_app(upstream):
    return build_full_app(
        host="127.0.0.1",
        port=8443,
        http_client=upstream,
        ws_connector=FakeWsConnector(FakeWS()),
    )


async def test_blobcast_paths_go_to_the_blobcast_upstream_not_ecast():
    """Before this, one upstream_base served everything, so playing a Party
    Pack 1-6 game meant hand-editing the setting to blobcast.jackboxgames.com
    - which then broke every Party Pack 7+ game until it was edited back."""
    upstream = RecordingUpstream()
    app = _blobcast_app(upstream)

    async with TestClient(TestServer(app)) as client:
        await client.get("/room")
        await client.post("/accessToken", data=b"{}")

    assert upstream.calls == [
        ("GET", "https://blobcast.jackboxgames.com/room"),
        ("POST", "https://blobcast.jackboxgames.com/accessToken"),
    ]


async def test_ecast_paths_still_go_to_ecast():
    """Regression guard: this work is explicitly not allowed to move the
    Ecast path, which is the one that currently works."""
    upstream = RecordingUpstream()
    app = _blobcast_app(upstream)

    async with TestClient(TestServer(app)) as client:
        await client.post("/api/v2/rooms", data=b"{}")
        await client.post("/tts/generate", data=b"{}")

    assert upstream.calls == [
        ("POST", "https://ecast.jackboxgames.com/api/v2/rooms"),
        ("POST", "https://ecast.jackboxgames.com/tts/generate"),
    ]


async def test_the_room_response_server_field_becomes_a_bare_local_hostname():
    """A HOSTNAME, never a host:port and never a URL.

    The packet capture settled why: the game uses this value as a hostname
    and appends port 38203 itself. "127.0.0.1:8443" and
    "https://127.0.0.1:8443" therefore became unresolvable names - each
    stalling the game on its own repeatable delay (5.13s / 7.35s / 8.67s)
    with nothing ever reaching the bridge. "localhost" resolves to 127.0.0.1
    with no hosts-file edit and is already a SAN on the bridge's leaf
    certificate, so the game's TLS check passes against the CA it trusts.

    If anyone ever puts a port or scheme back into this value, it fails here
    rather than in somebody's game."""
    upstream = RecordingUpstream(
        body=json.dumps(
            {"create": True, "server": "ecast-prod-use2.jackboxgames.com"}
        ).encode("utf-8")
    )
    app = _blobcast_app(upstream)

    async with TestClient(TestServer(app)) as client:
        served = (await (await client.get("/room")).json())["server"]

    assert served == "localhost"
    assert ":" not in served and "/" not in served


async def test_an_ecast_room_response_is_not_touched_by_the_blobcast_rewriter():
    """Ecast's "server" is a wss:// URL and belongs to
    rooms.rewrite_server_field, which (with rewrite=None, i.e. defaults on)
    turns it into this bridge's ws relay base. What must NOT happen is the
    blobcast rewriter also claiming the field and writing a bare host:port
    over it - two rewriters fighting over one key."""
    upstream = RecordingUpstream(body=ROOM_INFO_JSON)
    app = _blobcast_app(upstream)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/v2/rooms/ABCD")

        assert (await resp.json())["server"] == "wss://127.0.0.1:8443/ws"


async def test_socketio_on_the_main_port_is_not_the_blobcast_path():
    """The game's socket.io session goes to SOCKETIO_PORT, never here, so
    nothing on the main site should be trying to serve it. Pinned because an
    earlier step did register a probe on this path, and a leftover handler
    here would silently shadow the real listener."""
    upstream = RecordingUpstream()
    app = _blobcast_app(upstream)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/socket.io/1/?t=123")

        assert resp.status != 503, "the removed probe still appears to be registered"


# ---- the two browser-facing pages ---------------------------------------


async def test_a_browser_on_a_service_path_is_refused_not_forwarded():
    """A browser reload on /api is not a page view: the bridge would send it
    upstream under the game's identity, which is how empty rooms get created
    and how an address ends up rate-limited."""
    forwarded = []

    class Recording(FakeUpstreamClient):
        async def request(self, method, url, *, headers, data):
            forwarded.append(url)
            return await super().request(method, url, headers=headers, data=data)

    app = build_full_app(
        host="127.0.0.1",
        port=8443,
        http_client=Recording(UpstreamResponse(200, {}, ROOM_INFO_JSON)),
        ws_connector=FakeWsConnector(FakeWS()),
    )

    async with TestClient(TestServer(app)) as client:
        resp = await client.get(
            "/api/v2/rooms/ABCD", headers={"Accept": "text/html,application/xhtml+xml"}
        )

        assert resp.status == 403
        assert resp.content_type == "text/html"
        body = await resp.text()
        assert "служебный" in body
        assert "/api/v2/rooms/ABCD" in body, "the page must name the path that was refused"

    assert forwarded == [], "nothing may reach Jackbox from a browser"


async def test_a_browser_on_the_root_gets_the_calm_page_not_the_red_one():
    app = build_full_app(
        host="127.0.0.1",
        port=8443,
        http_client=FakeUpstreamClient(UpstreamResponse(200, {}, ROOM_INFO_JSON)),
        ws_connector=FakeWsConnector(FakeWS()),
    )

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/", headers={"Accept": "text/html"})

        assert resp.status == 200
        body = await resp.text()
        assert "служебный" not in body
        assert "у моста нет веб-интерфейса" in body


async def test_a_blobcast_path_opened_in_a_browser_is_also_refused():
    """Blobcast prefixes are service paths too - /room creates a real game
    session upstream."""
    app = build_full_app(
        host="127.0.0.1",
        port=8443,
        http_client=FakeUpstreamClient(UpstreamResponse(200, {}, b"{}")),
        ws_connector=FakeWsConnector(FakeWS()),
    )

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/room", headers={"Accept": "text/html"})

        assert resp.status == 403


async def test_the_game_is_never_handed_a_page_on_a_service_path():
    """The whole reason the path check lives INSIDE the wants_html branch:
    ahead of it, this request would get HTML instead of its JSON."""
    app = build_full_app(
        host="127.0.0.1",
        port=8443,
        http_client=FakeUpstreamClient(UpstreamResponse(200, {}, ROOM_INFO_JSON)),
        ws_connector=FakeWsConnector(FakeWS()),
    )

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/api/v2/rooms/ABCD", headers={"Accept": "*/*"})

        assert resp.status == 200
        # Proxied (and server-field rewritten) rather than answered with a page.
        assert b"apptag" in await resp.read()
        assert resp.content_type != "text/html"


async def test_favicon_is_answered_locally_instead_of_being_forwarded():
    """A browser asks for it on every visit with `Accept: image/...`, so it
    slips past the HTML check and used to be proxied to Jackbox, which answers
    403 - two real upstream round trips per idle browser visit."""
    forwarded = []

    class Recording(FakeUpstreamClient):
        async def request(self, method, url, *, headers, data):
            forwarded.append(url)
            return await super().request(method, url, headers=headers, data=data)

    app = build_full_app(
        host="127.0.0.1",
        port=8443,
        http_client=Recording(UpstreamResponse(200, {}, b"{}")),
        ws_connector=FakeWsConnector(FakeWS()),
    )

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/favicon.ico", headers={"Accept": "image/webp,*/*"})

        assert resp.status == 204

    assert forwarded == []
