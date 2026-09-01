import json

from aiohttp.test_utils import TestClient, TestServer

from bridgebox.server.factory import build_full_app
from bridgebox.server.rooms import UpstreamResponse, rewrite_server_field

REAL_RELAY = "wss://ecast-relay-prod-01.jackboxgames.com/ws"
LOCAL_WS = "wss://127.0.0.1:8443/ws"


class RecordingUpstreamClient:
    """Captures every forwarded request and replays a canned response."""

    def __init__(self, response: UpstreamResponse):
        self.response = response
        self.calls = []

    async def request(self, method, url, *, headers, data):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "data": data})
        return self.response


class FakeWS:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def send_str(self, data):
        self.sent.append(data)

    async def close(self):
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class FakeWsConnector:
    def __init__(self):
        self.connected_url = None

    async def connect(self, url):
        self.connected_url = url
        return FakeWS()


# ---- tolerant JSON rewriting -------------------------------------------


def test_rewrite_handles_wrapped_response_envelope():
    """Real Ecast responses may wrap the payload (e.g. {"ok":true,"body":{...}}).
    The research docs only showed the flat shape, so the rewrite must find
    "server" wherever it actually lives rather than assuming top level."""
    body = json.dumps({"ok": True, "body": {"roomid": "ABCD", "server": REAL_RELAY}}).encode()

    new_body, original, room_id = rewrite_server_field(body, local_ws_base=LOCAL_WS)

    parsed = json.loads(new_body)
    assert parsed["body"]["server"] == LOCAL_WS
    assert parsed["ok"] is True
    assert original == REAL_RELAY
    assert room_id == "ABCD"


def test_rewrite_accepts_camel_case_room_id_key():
    body = json.dumps({"roomId": "WXYZ", "server": REAL_RELAY}).encode()

    _, _, room_id = rewrite_server_field(body, local_ws_base=LOCAL_WS)

    assert room_id == "WXYZ"


def test_rewrite_accepts_code_room_id_key():
    body = json.dumps({"code": "QRST", "server": REAL_RELAY}).encode()

    _, _, room_id = rewrite_server_field(body, local_ws_base=LOCAL_WS)

    assert room_id == "QRST"


def test_rewrite_only_touches_websocket_urls():
    """A "server" field that isn't a ws/wss URL isn't a relay address and
    must be left alone - rewriting it blindly would corrupt the payload."""
    body = json.dumps({"server": "some-server-name", "roomid": "ABCD"}).encode()

    new_body, original, _ = rewrite_server_field(body, local_ws_base=LOCAL_WS)

    assert json.loads(new_body)["server"] == "some-server-name"
    assert original is None


def test_rewrite_handles_list_nesting():
    body = json.dumps({"rooms": [{"roomid": "ABCD", "server": REAL_RELAY}]}).encode()

    new_body, original, room_id = rewrite_server_field(body, local_ws_base=LOCAL_WS)

    assert json.loads(new_body)["rooms"][0]["server"] == LOCAL_WS
    assert original == REAL_RELAY
    assert room_id == "ABCD"


# ---- universal API proxying --------------------------------------------


def _make_app(response: UpstreamResponse):
    client = RecordingUpstreamClient(response)
    connector = FakeWsConnector()
    app = build_full_app(
        host="127.0.0.1", port=8443, http_client=client, ws_connector=connector
    )
    return app, client, connector


async def test_unknown_api_path_is_proxied_not_answered_with_html():
    """The whole point of a universal bridge is not needing a hardcoded route
    per endpoint - any /api/** path the game calls must reach the real API."""
    response = UpstreamResponse(
        status=200, headers={"Content-Type": "application/json"}, body=b'{"ok":true}'
    )
    app, client, _ = _make_app(response)

    async with TestClient(TestServer(app)) as http:
        resp = await http.get("/api/v2/audience/ABCD")
        assert resp.status == 200
        assert "BridgeBox" not in await resp.text()

    assert client.calls[0]["url"] == "https://ecast.jackboxgames.com/api/v2/audience/ABCD"


async def test_api_proxy_forwards_arbitrary_methods():
    response = UpstreamResponse(status=204, headers={}, body=b"")
    app, client, _ = _make_app(response)

    async with TestClient(TestServer(app)) as http:
        await http.delete("/api/v2/rooms/ABCD")

    assert client.calls[0]["method"] == "DELETE"


async def test_api_proxy_preserves_query_string():
    response = UpstreamResponse(status=200, headers={}, body=b"{}")
    app, client, _ = _make_app(response)

    async with TestClient(TestServer(app)) as http:
        await http.get("/api/v2/rooms/ABCD?userId=42")

    assert client.calls[0]["url"].endswith("/api/v2/rooms/ABCD?userId=42")


async def test_api_proxy_drops_upstream_content_length_header():
    """Body length changes when "server" is rewritten - replaying upstream's
    Content-Length would truncate or hang the client."""
    body = json.dumps({"roomid": "ABCD", "server": REAL_RELAY}).encode()
    response = UpstreamResponse(
        status=200,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
        body=body,
    )
    app, _, _ = _make_app(response)

    async with TestClient(TestServer(app)) as http:
        resp = await http.get("/api/v2/rooms/ABCD")
        payload = await resp.json()

    assert payload["server"] == LOCAL_WS


async def test_proxy_supplies_user_agent_when_client_sends_none():
    """Verified against the live API: no browser-like User-Agent means the AWS
    load balancer answers 403 before the request reaches Ecast at all."""
    from bridgebox.server.rooms import RoomsProxy

    client = RecordingUpstreamClient(UpstreamResponse(status=200, headers={}, body=b"{}"))
    proxy = RoomsProxy(
        upstream_base="https://ecast.jackboxgames.com",
        local_ws_base=LOCAL_WS,
        http_client=client,
        room_relays={},
    )

    await proxy.forward("GET", "/api/v2/rooms/ABCD", headers={}, data=None)

    assert client.calls[0]["headers"]["User-Agent"].startswith("Mozilla/")


async def test_proxy_keeps_the_games_own_user_agent():
    from bridgebox.server.rooms import RoomsProxy

    client = RecordingUpstreamClient(UpstreamResponse(status=200, headers={}, body=b"{}"))
    proxy = RoomsProxy(
        upstream_base="https://ecast.jackboxgames.com",
        local_ws_base=LOCAL_WS,
        http_client=client,
        room_relays={},
    )

    await proxy.forward(
        "GET", "/api/v2/rooms/ABCD", headers={"User-Agent": "JackboxGame/1.0"}, data=None
    )

    assert client.calls[0]["headers"]["User-Agent"] == "JackboxGame/1.0"


async def test_non_api_path_serves_the_browser_warning_only_to_a_browser():
    """Used to be served to everything that missed /api - which is how
    FixyText's POST /tts/generate got an HTML page instead of its TTS
    result. Now gated on the caller being able to render it."""
    response = UpstreamResponse(status=200, headers={}, body=b"{}")
    app, _, _ = _make_app(response)

    async with TestClient(TestServer(app)) as http:
        resp = await http.get("/", headers={"Accept": "text/html"})
        assert "у моста нет веб-интерфейса" in await resp.text()

        # The game's libcurl sends Accept: */* - it gets a real proxied reply.
        game = await http.get("/", headers={"Accept": "*/*"})
        assert "у моста нет веб-интерфейса" not in await game.text()


# ---- log redaction ------------------------------------------------------


def test_room_token_is_redacted_from_logged_bodies():
    """The real room-creation response carries the credential that controls
    the room. Headers were already redacted while bodies went to the log
    verbatim - and that log is written to disk, shown behind a "Копировать"
    button, and pasted into bug reports."""
    from bridgebox.server.rooms import _preview

    body = json.dumps(
        {"ok": True, "body": {"host": "ecast-prod-use2.jackboxgames.com",
                              "code": "MNAK", "token": "670f3779de7658e56fb5306e"}}
    ).encode()

    line = _preview(body)

    assert "670f3779de7658e56fb5306e" not in line
    assert '"token":"<hidden>"' in line.replace(" ", "")
    # Everything else must survive - the preview is a debugging tool.
    assert "MNAK" in line
    assert "ecast-prod-use2.jackboxgames.com" in line


def test_redaction_survives_a_body_truncated_mid_json():
    """Previews are cut at BODY_PREVIEW_CHARS, so the redactor routinely sees
    invalid JSON. It must not throw on the payloads most worth reading."""
    from bridgebox.server.rooms import _preview

    body = (b'{"token": "abcdef0123456789", "blob": "' + b"x" * 5000)

    line = _preview(body)

    assert "abcdef0123456789" not in line
    assert "+" in line  # truncation marker still appended


def test_ws_frames_are_redacted_too():
    from bridgebox.server.relay import _frame_preview

    frame = json.dumps({"opcode": "client/welcome", "token": "secret-token-value"})

    line = _frame_preview(frame)

    assert "secret-token-value" not in line
    assert "client/welcome" in line


def test_binary_bodies_are_summarised_instead_of_decoded_as_text():
    """errors="replace" turned a binary body into 800 characters of
    replacement characters in both the log file and the console. The body
    confirmed to hit this is the audio /tts/generate returns; uploaded
    avatars and voice clips would do the same. Size and type are the only
    useful facts about a binary body."""
    from bridgebox.server.rooms import _preview

    audio = b"\xff\xfb\x90\x00" + bytes(range(256)) * 8

    line = _preview(audio, content_type="audio/mpeg")

    assert line == f"<{len(audio)} bytes of audio/mpeg>"
    assert "�" not in line


def test_json_bodies_are_still_shown_in_full_detail():
    """The guard must not hide the payloads this log exists to show."""
    from bridgebox.server.rooms import _preview

    body = json.dumps({"code": "MNAK", "host": "ecast-prod-use2.jackboxgames.com"}).encode()

    assert "MNAK" in _preview(body, content_type="application/json; charset=utf-8")
    # No Content-Type at all still reads as text - Ecast is JSON throughout.
    assert "MNAK" in _preview(body, content_type=None)


def test_upstream_timeout_bounds_a_stalled_read_not_the_whole_transfer():
    """A total cap covers reading the body too, so it would cut off a large
    transfer that is progressing perfectly well. What needs bounding is a
    connection that has stopped sending. Defensive: no observed request has
    come close, but a proxy must not put a clock on how big a payload may be."""
    from bridgebox.server.rooms import AiohttpUpstreamClient

    client = AiohttpUpstreamClient(session=object())

    assert client._timeout.total is None, "a total cap kills healthy large transfers"
    assert client._timeout.sock_read == 20.0
    assert client._timeout.connect == 10.0


async def test_upstream_client_records_when_a_request_was_last_made():
    """RuntimeCore._health_check_loop reads last_request_at to skip a health
    check round right after real game traffic - see RECENT_ACTIVITY_SKIP_S."""
    import time

    from bridgebox.server.rooms import AiohttpUpstreamClient

    class FakeResponse:
        status = 200
        headers: dict = {}

        async def read(self):
            return b"{}"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class FakeSession:
        def request(self, method, url, *, headers, data, timeout):
            return FakeResponse()

    client = AiohttpUpstreamClient(session=FakeSession())
    assert client.last_request_at is None

    before = time.monotonic()
    await client.request("GET", "https://example.com", headers={}, data=None)
    after = time.monotonic()

    assert client.last_request_at is not None
    assert before <= client.last_request_at <= after
