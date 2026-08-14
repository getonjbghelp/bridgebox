import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from bridgebox.server.rooms import (
    RoomsProxy,
    UpstreamResponse,
    register_api_proxy,
    rewrite_server_field,
)

ROOM_INFO_JSON = json.dumps(
    {
        "apptag": "fibbage3",
        "appid": "38b0be6e-346c-4860-912b-cb8e96bfbc41",
        "roomid": "ABCD",
        "server": "wss://ecast-relay-prod-01.jackboxgames.com/ws",
    }
).encode("utf-8")


def test_rewrite_server_field_replaces_wss_url():
    new_body, original_server, room_id = rewrite_server_field(
        ROOM_INFO_JSON, local_ws_base="wss://127.0.0.1:8443/ws"
    )

    parsed = json.loads(new_body)
    assert parsed["server"] == "wss://127.0.0.1:8443/ws"
    assert parsed["roomid"] == "ABCD"  # untouched
    assert parsed["apptag"] == "fibbage3"  # untouched
    assert original_server == "wss://ecast-relay-prod-01.jackboxgames.com/ws"
    assert room_id == "ABCD"


def test_rewrite_server_field_no_op_when_no_server_key():
    body = json.dumps({"roomid": "ABCD"}).encode("utf-8")

    new_body, original_server, room_id = rewrite_server_field(
        body, local_ws_base="wss://127.0.0.1:8443/ws"
    )

    assert json.loads(new_body) == {"roomid": "ABCD"}
    assert original_server is None
    assert room_id == "ABCD"


def test_rewrite_server_field_non_json_body_passthrough():
    body = b"not json at all"

    new_body, original_server, room_id = rewrite_server_field(
        body, local_ws_base="wss://127.0.0.1:8443/ws"
    )

    assert new_body == body
    assert original_server is None
    assert room_id is None


# Captured live: creating a room (POST /api/v2/rooms) returns a bare relay
# hostname under "host", envelope-wrapped, room code under "code" - a
# different shape than the "server"/"roomid" example above, confirmed from a
# real game session (apptag "fourbage").
ROOM_CREATE_HOST_SHAPE_JSON = json.dumps(
    {
        "ok": True,
        "body": {
            "host": "ecast-prod-use2.jackboxgames.com",
            "code": "MNAK",
            "token": "670f3779de7658e56fb5306e",
        },
    }
).encode("utf-8")


def test_rewrite_server_field_leaves_the_bare_host_shape_untouched():
    """A bare "host" field (room creation's response shape) is deliberately
    NOT rewritten - see the comment on _walk_and_rewrite. Confirmed against
    real traffic: rewriting it pointed the game at this bridge's relay,
    which never completes a WS handshake, instead of the *unrewritten* real
    host - already on zapret's own hostlist, so DPI bypass for it already
    happens at the packet level with no help needed from this bridge.
    Room-code extraction (via "code") must still work regardless."""
    new_body, original_server, room_id = rewrite_server_field(
        ROOM_CREATE_HOST_SHAPE_JSON, local_ws_base="wss://127.0.0.1:8443/ws"
    )

    assert new_body == ROOM_CREATE_HOST_SHAPE_JSON  # byte-identical passthrough
    assert original_server is None
    assert room_id == "MNAK"


class FakeUpstreamClient:
    def __init__(self, response: UpstreamResponse):
        self._response = response
        self.calls = []

    async def request(self, method, url, *, headers, data):
        self.calls.append(
            {"method": method, "url": url, "headers": dict(headers), "data": data}
        )
        return self._response


async def test_forward_and_rewrite_sets_host_and_origin_headers():
    fake_client = FakeUpstreamClient(
        UpstreamResponse(status=200, headers={"content-type": "application/json"}, body=ROOM_INFO_JSON)
    )
    room_relays: dict[str, str] = {}
    proxy = RoomsProxy(
        upstream_base="https://ecast.jackboxgames.com",
        local_ws_base="wss://127.0.0.1:8443/ws",
        http_client=fake_client,
        room_relays=room_relays,
    )

    await proxy.forward("POST", "/api/v2/rooms", headers={"Host": "127.0.0.1:8443"}, data=b"{}")

    call = fake_client.calls[0]
    assert call["headers"]["Host"] == "ecast.jackboxgames.com"
    assert call["headers"]["Origin"] == "https://jackbox.tv"
    assert call["url"] == "https://ecast.jackboxgames.com/api/v2/rooms"


async def test_forward_strips_incoming_host_header_regardless_of_case():
    fake_client = FakeUpstreamClient(
        UpstreamResponse(status=200, headers={"content-type": "application/json"}, body=ROOM_INFO_JSON)
    )
    proxy = RoomsProxy(
        upstream_base="https://ecast.jackboxgames.com",
        local_ws_base="wss://127.0.0.1:8443/ws",
        http_client=fake_client,
        room_relays={},
    )

    await proxy.forward(
        "POST",
        "/api/v2/rooms",
        headers={"host": "127.0.0.1:8443", "origin": "https://localhost:8443"},
        data=b"{}",
    )

    call_headers = fake_client.calls[0]["headers"]
    # exactly one Host/Origin entry each, not a stray lowercase leftover
    assert [k for k in call_headers if k.lower() == "host"] == ["Host"]
    assert [k for k in call_headers if k.lower() == "origin"] == ["Origin"]
    assert call_headers["Host"] == "ecast.jackboxgames.com"
    assert call_headers["Origin"] == "https://jackbox.tv"


async def test_forward_and_rewrite_returns_rewritten_body_and_records_relay():
    fake_client = FakeUpstreamClient(
        UpstreamResponse(status=200, headers={"content-type": "application/json"}, body=ROOM_INFO_JSON)
    )
    room_relays: dict[str, str] = {}
    proxy = RoomsProxy(
        upstream_base="https://ecast.jackboxgames.com",
        local_ws_base="wss://127.0.0.1:8443/ws",
        http_client=fake_client,
        room_relays=room_relays,
    )

    resp = await proxy.forward("GET", "/api/v2/rooms/ABCD", headers={}, data=None)

    assert resp.status == 200
    parsed = json.loads(resp.body)
    assert parsed["server"] == "wss://127.0.0.1:8443/ws"
    assert room_relays["ABCD"] == "wss://ecast-relay-prod-01.jackboxgames.com/ws"


async def test_forward_and_rewrite_passthrough_on_upstream_error():
    fake_client = FakeUpstreamClient(
        UpstreamResponse(status=404, headers={"content-type": "text/plain"}, body=b"not found")
    )
    room_relays: dict[str, str] = {}
    proxy = RoomsProxy(
        upstream_base="https://ecast.jackboxgames.com",
        local_ws_base="wss://127.0.0.1:8443/ws",
        http_client=fake_client,
        room_relays=room_relays,
    )

    resp = await proxy.forward("GET", "/api/v2/rooms/ZZZZ", headers={}, data=None)

    assert resp.status == 404
    assert resp.body == b"not found"
    assert room_relays == {}


async def test_register_api_proxy_wires_post_and_get():
    fake_client = FakeUpstreamClient(
        UpstreamResponse(status=200, headers={"content-type": "application/json"}, body=ROOM_INFO_JSON)
    )
    room_relays: dict[str, str] = {}
    proxy = RoomsProxy(
        upstream_base="https://ecast.jackboxgames.com",
        local_ws_base="wss://127.0.0.1:8443/ws",
        http_client=fake_client,
        room_relays=room_relays,
    )

    # API proxy must be registered before the browser stub - both are
    # wildcards, and the stub would otherwise shadow every API call.
    app = web.Application()
    register_api_proxy(app, proxy)

    async with TestClient(TestServer(app)) as client:
        post_resp = await client.post("/api/v2/rooms", data=b"{}")
        assert post_resp.status == 200
        post_body = await post_resp.json()
        assert post_body["server"] == "wss://127.0.0.1:8443/ws"

        get_resp = await client.get("/api/v2/rooms/ABCD")
        assert get_resp.status == 200
        get_body = await get_resp.json()
        assert get_body["server"] == "wss://127.0.0.1:8443/ws"

    assert room_relays["ABCD"] == "wss://ecast-relay-prod-01.jackboxgames.com/ws"


async def test_register_api_proxy_returns_json_502_when_upstream_is_unreachable():
    """Reproduces a real DPI-block/offline capture: the upstream connect
    raises instead of returning any HTTP response. Before this, that
    exception reached aiohttp unhandled - a bare 500 with a plain-text body
    (every other error path here returns JSON) and a full traceback logged
    as if it were a crash, for what is this bridge's single most expected
    operational condition."""

    class RaisingUpstreamClient:
        async def request(self, method, url, *, headers, data):
            raise ConnectionError("Cannot connect to host ecast.jackboxgames.com:443 ssl:default")

    room_relays: dict[str, str] = {}
    proxy = RoomsProxy(
        upstream_base="https://ecast.jackboxgames.com",
        local_ws_base="wss://127.0.0.1:8443/ws",
        http_client=RaisingUpstreamClient(),
        room_relays=room_relays,
    )

    app = web.Application()
    register_api_proxy(app, proxy)

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/v2/rooms", data=b"{}")

        assert resp.status == 502
        assert resp.headers["Content-Type"] == "application/json"
        body = await resp.json()  # must not raise - the whole point is a parseable body
        assert body["ok"] is False
        assert "ecast.jackboxgames.com" in body["error"]


# ---- log detail ---------------------------------------------------------


def test_sensitive_headers_are_hidden_from_logs():
    """Logs get pasted into bug reports. Session cookies and auth tokens must
    not travel with them, while everything else stays readable."""
    from bridgebox.server.rooms import _headers_for_log

    rendered = _headers_for_log(
        {
            "User-Agent": "Mozilla/5.0",
            "Cookie": "session=super-secret-value",
            "Authorization": "Bearer abc.def.ghi",
            "Content-Type": "application/json",
        }
    )

    assert "super-secret-value" not in rendered
    assert "abc.def.ghi" not in rendered
    assert "<hidden" in rendered
    # Non-sensitive headers are the reason the line exists at all.
    assert "User-Agent=Mozilla/5.0" in rendered
    assert "Content-Type=application/json" in rendered


def test_body_preview_truncates_instead_of_flooding_the_log():
    from bridgebox.server.rooms import _preview

    assert _preview(b"") == "<empty>"
    assert _preview(None) == "<empty>"
    assert _preview(b'{"ok": true}') == '{"ok": true}'

    long_body = b"x" * 5000
    rendered = _preview(long_body, limit=100)
    assert len(rendered) < 200
    assert "+4900 bytes" in rendered


@pytest.mark.asyncio
async def test_connection_scoped_headers_are_not_replayed_upstream():
    """RFC 7230 6.1: a proxy strips connection-scoped headers on every hop.

    These were forwarded verbatim while aiohttp independently generated its
    own, so a request could reach the upstream carrying both the game's
    Content-Length and aiohttp's Transfer-Encoding - the disagreement between
    two servers that request smuggling is built out of."""
    captured = {}

    class RecordingClient:
        async def request(self, method, url, *, headers, data):
            captured.update(headers)
            return UpstreamResponse(status=200, headers={}, body=b"{}")

    proxy = RoomsProxy(
        upstream_base="https://upstream.example",
        local_ws_base="wss://127.0.0.1:8443/ws",
        http_client=RecordingClient(),
        room_relays={},
    )

    await proxy.forward(
        "POST",
        "/api/v2/rooms",
        headers={
            "Content-Length": "9",
            "Transfer-Encoding": "chunked",
            "Connection": "keep-alive",
            "Keep-Alive": "timeout=5",
            "Upgrade": "h2c",
            "Proxy-Connection": "keep-alive",
            "TE": "trailers",
            "Trailer": "Expires",
            "X-Correlation-Id": "keep-me",
        },
        data=b'{"ok":true}',
    )

    lowered = {k.lower() for k in captured}
    for banned in (
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "upgrade",
        "proxy-connection",
        "te",
        "trailer",
    ):
        assert banned not in lowered, f"{banned} was replayed upstream"
    # An ordinary header still travels - this is a targeted strip, not a purge.
    assert captured["X-Correlation-Id"] == "keep-me"


@pytest.mark.asyncio
async def test_a_compressed_request_body_keeps_its_content_encoding():
    """Content-Encoding is NOT connection-scoped, and the request body is
    forwarded byte-identical - stripping it would leave the upstream unable to
    decode a body the game legitimately compressed."""
    captured = {}

    class RecordingClient:
        async def request(self, method, url, *, headers, data):
            captured.update(headers)
            return UpstreamResponse(status=200, headers={}, body=b"{}")

    proxy = RoomsProxy(
        upstream_base="https://upstream.example",
        local_ws_base="wss://127.0.0.1:8443/ws",
        http_client=RecordingClient(),
        room_relays={},
    )

    await proxy.forward(
        "POST", "/api/v2/rooms", headers={"Content-Encoding": "gzip"}, data=b"\x1f\x8b"
    )

    assert captured["Content-Encoding"] == "gzip"
