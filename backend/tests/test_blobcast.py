"""Blobcast (Party Pack 1-6 + some singles, "API v1") alongside Ecast.

The two protocols share the bridge but never share a path, which is what lets
one listener serve both with nothing to switch: Ecast lives under /api/v2/*,
Blobcast under /room, /accessToken and /socket.io/*.

Most of what these tests assert was measured against the live game rather
than reasoned out, and the docstrings say which measurement, because several
plausible-sounding versions were wrong first: the "server" field takes a
bare hostname and the game appends port 38203 itself; the WS upgrade carries
no User-Agent at all, so one has to be supplied rather than relayed; and
that port sat outside the zapret filter, leaving the session with no DPI
bypass. Confirmed working afterwards - a full round with three players.
"""
import json

import pytest

from bridgebox.server import blobcast


# ---- path classification --------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/room", "/accessToken", "/socket.io/1/", "/socket.io/1/websocket/abc", "/blobcast/room"],
)
def test_blobcast_paths_are_recognised(path):
    assert blobcast.is_blobcast_path(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "/api/v2/rooms",
        "/api/v2/rooms/ABCD",
        "/api/v2/app-configs/fourbage",
        "/tts/generate",
        "/",
        # Segment-aware, same reasoning as rooms.path_is_forwarded: a bare
        # startswith would let "/room" also claim "/roomservice", which would
        # be a different endpoint entirely.
        "/roomservice",
        "/accessTokenFoo",
    ],
)
def test_non_blobcast_paths_are_not_claimed(path):
    assert blobcast.is_blobcast_path(path) is False


# ---- the "server" field ---------------------------------------------------


def test_room_response_server_is_rewritten_to_the_bridge():
    """The exact shape seen in the real log: GET /room answers with a bare
    hostname, and the game then takes its whole socket.io session there -
    which is why nothing after /accessToken has ever reached this bridge."""
    body = json.dumps(
        {"create": True, "server": "ecast-prod-use2.jackboxgames.com"}
    ).encode("utf-8")

    new_body, real = blobcast.rewrite_room_response(body, local_host="127.0.0.1:8443")

    assert real == "ecast-prod-use2.jackboxgames.com"
    assert json.loads(new_body) == {"create": True, "server": "127.0.0.1:8443"}


def test_a_ws_url_server_field_is_left_alone():
    """Ecast's "server" is a full wss://.../ws URL and belongs to
    rooms.rewrite_server_field. Claiming it here as well would mean two
    rewriters fighting over one field - and the Ecast path is explicitly not
    being touched by this work."""
    body = json.dumps({"server": "wss://ecast-relay.jackboxgames.com/ws"}).encode("utf-8")

    new_body, real = blobcast.rewrite_room_response(body, local_host="127.0.0.1:8443")

    assert real is None
    assert new_body == body


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"not json at all",
        json.dumps({"create": True}).encode("utf-8"),
        json.dumps({"server": ""}).encode("utf-8"),
        json.dumps({"server": 42}).encode("utf-8"),
    ],
)
def test_nothing_to_rewrite_passes_through_byte_identical(body):
    new_body, real = blobcast.rewrite_room_response(body, local_host="127.0.0.1:8443")

    assert real is None
    assert new_body == body


# ---- remembering where the session really belongs -------------------------


def test_the_real_server_is_remembered_for_the_relay_to_use():
    sessions = blobcast.BlobcastSessions()
    assert sessions.upstream is None

    sessions.remember("ecast-prod-use2.jackboxgames.com")

    assert sessions.upstream == "ecast-prod-use2.jackboxgames.com"


def test_a_later_room_replaces_the_earlier_one():
    """One bridge hosts one game at a time, so a single slot is the whole
    requirement - a room-code-keyed map would be answering a question nobody
    is asking yet (the /room response carries no room code; the code only
    shows up later in the /accessToken request body)."""
    sessions = blobcast.BlobcastSessions()

    sessions.remember("first.jackboxgames.com")
    sessions.remember("second.jackboxgames.com")

    assert sessions.upstream == "second.jackboxgames.com"


# ---- the handler ----------------------------------------------------------


class _StubResponse:
    def __init__(self, body: bytes):
        self.body = body
        self.status = 200
        self.headers = {"Content-Type": "application/json"}


def _handler(local_host=blobcast.LOCAL_SERVER_NAME):
    room_json = json.dumps(
        {"create": True, "server": "ecast-prod-use2.jackboxgames.com"}
    ).encode("utf-8")

    async def api_handler(request):
        return _StubResponse(room_json)

    sessions = blobcast.BlobcastSessions()
    return blobcast.create_blobcast_handler(api_handler, sessions, local_host), sessions


async def test_the_handler_rewrites_and_records_in_one_pass():
    """Both halves matter: the game must be sent to us, and the relay must
    still know where the room really lives to forward the session on."""
    handle, sessions = _handler()

    response = await handle(object())

    assert json.loads(response.body)["server"] == blobcast.LOCAL_SERVER_NAME
    assert sessions.upstream == "ecast-prod-use2.jackboxgames.com"


# ---- the socket.io interceptor on port 38203 -----------------------------
#
# The packet capture settled what three game sessions of guessing could not:
# the game takes ONLY the hostname out of the "server" field and connects to
# it on port 38203 over TLS (SNI ecast-prod-use2.jackboxgames.com). Neither
# /room nor /accessToken carries that port - 60 and 66 bytes, fully accounted
# for by their known fields - and it appears in no .exe/.dll/.jet/.json in the
# game folder, so it is the game's own constant and cannot be moved from the
# server side.
#
# That makes the interception straightforward and, better, system-clean:
# serve "localhost" as the hostname (it resolves to 127.0.0.1 with no hosts
# file edit, and is already a SAN on the bridge's existing certificate) and
# listen on 38203. The earlier attempts failed not because of the value
# format but because nothing was ever listening on the port the game uses.


class _FakeUpstream:
    def __init__(self):
        self.calls = []

    async def request(self, method, url, *, headers, data):
        from bridgebox.server.rooms import UpstreamResponse

        self.calls.append((method, url))
        return UpstreamResponse(status=200, headers={"Content-Type": "text/plain"}, body=b"ok")


def test_socketio_port_is_the_one_the_capture_showed():
    assert blobcast.SOCKETIO_PORT == 38203


async def test_socketio_http_request_is_forwarded_to_the_remembered_upstream():
    from aiohttp.test_utils import TestClient, TestServer

    sessions = blobcast.BlobcastSessions()
    sessions.remember("ecast-prod-use2.jackboxgames.com")
    upstream = _FakeUpstream()

    app = blobcast.build_socketio_app(sessions, upstream, ws_connector=None)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/socket.io/1/?t=123")
        assert resp.status == 200

    assert upstream.calls == [
        ("GET", "https://ecast-prod-use2.jackboxgames.com:38203/socket.io/1/?t=123")
    ]


async def test_socketio_without_a_known_upstream_is_refused_loudly():
    """The room's /room call must have gone through the bridge first. Failing
    with a JSON 503 beats guessing a host, which would connect the game to
    something nobody chose."""
    from aiohttp.test_utils import TestClient, TestServer

    app = blobcast.build_socketio_app(blobcast.BlobcastSessions(), _FakeUpstream(), None)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/socket.io/1/")
        assert resp.status == 503


# ---- the second listener is actually started -----------------------------


async def test_runtime_start_also_listens_on_the_socketio_port(tmp_path):
    """The game's socket.io session goes to a port IT chooses (38203), not the
    one the bridge is configured with, so the main site cannot serve it. If
    this listener is never started, that half of the session silently goes
    nowhere - precisely the failure that cost several diagnostic runs before
    a packet capture found the port.

    Drives the real RuntimeCore, with only its injected boundaries faked, so
    it fails if the wiring is removed rather than if this test's own scaffold
    changes."""
    from bridgebox.config import Config
    from bridgebox.runtime_core import RuntimeCore
    from bridgebox.server.factory import build_full_app
    from tests.test_runtime_core import _make_deps

    calls, deps = _make_deps(tmp_path)
    # The stock fake returns a plain object(); a real app is needed for the
    # socket.io site to be discoverable on it.
    deps["build_full_app"] = lambda **kw: build_full_app(**kw)

    config = Config()
    config.zapret.enabled = False
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)

    await core.start()
    try:
        ports = [call["port"] for call in calls["run_server"]]
        assert ports == [config.server.port, blobcast.SOCKETIO_PORT]
        # Same TLS material for both, so the game's cert check passes on the
        # socket.io port too - it is "localhost", already a SAN on the leaf.
        assert calls["run_server"][0]["ssl"] is calls["run_server"][1]["ssl"]
    finally:
        await core.stop()


# ---- the game's identity has to survive the upstream WS handshake --------


class _RecordingWsConnector:
    def __init__(self):
        self.url = None
        self.headers = None

    async def connect(self, url, *, headers=None):
        self.url = url
        self.headers = headers

        class _Ws:
            closed = False

            async def send_str(self, data): ...
            async def close(self): ...
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration

        return _Ws()


async def test_the_upstream_never_sees_our_own_python_user_agent():
    """The defect itself, stated as an invariant.

    Jackbox answers 403 to aiohttp's default "Python/3.x aiohttp/..." on this
    upgrade - the same non-browser rejection rooms.py documents for the HTTP
    path - and that is what stopped rooms being created. Whatever else
    changes, our own client identity must never be what goes upstream."""
    from aiohttp.test_utils import TestClient, TestServer

    sessions = blobcast.BlobcastSessions()
    sessions.remember("host.example")
    connector = _RecordingWsConnector()

    app = blobcast.build_socketio_app(sessions, _FakeUpstream(), connector)
    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect("/socket.io/1/websocket/abc")
        await ws.close()

    sent = connector.headers.get("User-Agent", "")
    assert sent, "upstream WS was opened without a User-Agent"
    assert "aiohttp" not in sent.lower() and "python" not in sent.lower()


async def test_ws_handshake_headers_are_not_replayed_upstream():
    """Sec-WebSocket-Key and friends belong to the client<->bridge handshake.
    Replaying them onto the bridge<->Jackbox one makes aiohttp generate a
    conflicting handshake, so they are dropped the same way rooms.py drops
    hop-by-hop headers."""
    from aiohttp.test_utils import TestClient, TestServer

    sessions = blobcast.BlobcastSessions()
    sessions.remember("host.example")
    connector = _RecordingWsConnector()

    app = blobcast.build_socketio_app(sessions, _FakeUpstream(), connector)
    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect("/socket.io/1/websocket/abc")
        await ws.close()

    lowered = {k.lower() for k in connector.headers}
    assert not (lowered & {
        "upgrade", "connection", "sec-websocket-key",
        "sec-websocket-version", "sec-websocket-extensions", "host",
    })


# ---- the game sends no User-Agent on the WS upgrade ----------------------


async def test_a_user_agent_is_supplied_when_the_game_sends_none():
    """Measured, not assumed: the live log shows

        BLOBCAST WS upstream headers: {}
        GET /socket.io/1/websocket/... HTTP/1.1" 101 0 "-" "-"

    - the game's old socket.io client sets no User-Agent on the upgrade at
    all. Forwarding "whatever the game sent" therefore sent nothing, aiohttp
    filled in its own "Python/3.x aiohttp/...", and Jackbox answered 403.
    Reproduced offline: the same upgrade carrying the game's User-Agent gets
    101. So one has to be supplied, not merely relayed - the same reason
    rooms.py keeps FALLBACK_USER_AGENT for the HTTP path."""
    from aiohttp.test_utils import TestClient, TestServer

    sessions = blobcast.BlobcastSessions()
    sessions.remember("ecast-prod-use2.jackboxgames.com")
    sessions.remember_user_agent("JackboxGames/1.00 libcurl/7.57.0-DEV")
    connector = _RecordingWsConnector()

    app = blobcast.build_socketio_app(sessions, _FakeUpstream(), connector)
    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect("/socket.io/1/websocket/abc")
        await ws.close()

    assert connector.headers.get("User-Agent") == "JackboxGames/1.00 libcurl/7.57.0-DEV"


async def test_a_never_seen_game_still_gets_a_plausible_user_agent():
    """If /room never went through this bridge we have no recorded value, and
    sending none is the exact thing that gets refused."""
    from aiohttp.test_utils import TestClient, TestServer
    from bridgebox.server.rooms import FALLBACK_USER_AGENT

    sessions = blobcast.BlobcastSessions()
    sessions.remember("host.example")
    connector = _RecordingWsConnector()

    app = blobcast.build_socketio_app(sessions, _FakeUpstream(), connector)
    async with TestClient(TestServer(app)) as client:
        ws = await client.ws_connect("/socket.io/1/websocket/abc")
        await ws.close()

    assert connector.headers.get("User-Agent") == FALLBACK_USER_AGENT


async def test_the_room_request_user_agent_is_recorded_for_later():
    """The upgrade carries no User-Agent, but GET /room does - that is where
    the game's real identity is available to capture."""
    handle, sessions = _handler(local_host="localhost")

    class _Req:
        headers = {"User-Agent": "JackboxGames/1.00 libcurl/7.57.0-DEV"}

    await handle(_Req())

    assert sessions.user_agent == "JackboxGames/1.00 libcurl/7.57.0-DEV"


# ---- the upstream host from the wire is not trusted ----


@pytest.mark.parametrize(
    "hostile",
    [
        # Everything before "@" is userinfo, so the URL built from this
        # resolves to evil.example.com - measured with yarl, not assumed.
        "ecast.jackboxgames.com@evil.example.com",
        "evil.example.com/../..",
        "evil.example.com:443",
        "evil.example.com?x=1",
        "evil.example.com#frag",
        "has space",
        "",
        "a" * 300,
    ],
)
def test_a_server_value_that_is_not_a_bare_hostname_is_refused(hostile):
    """local_server_name - which the USER types - is validated strictly, while
    this value arrives from the network and was trusted completely. It is
    interpolated straight into the socket.io listener's upstream URL."""
    sessions = blobcast.BlobcastSessions()

    sessions.remember(hostile)

    assert sessions.upstream is None


@pytest.mark.parametrize(
    "good", ["ecast-prod-use2.jackboxgames.com", "localhost", "blobcast.jackboxgames.com"]
)
def test_a_real_relay_hostname_is_still_remembered(good):
    sessions = blobcast.BlobcastSessions()

    sessions.remember(good)

    assert sessions.upstream == good
