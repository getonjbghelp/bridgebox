import asyncio
import logging

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bridgebox.server.app import build_app
from bridgebox.server.relay import (
    WS_CLOSE_REASON_MAX_BYTES,
    _close_reason_bytes,
    create_relay_handler,
    register_relay_route,
    pump,
    relay,
)


class FakeWS:
    """In-memory stand-in for aiohttp's WS types, implementing just enough
    of the interface (__aiter__/send_str/close/closed) for pump()/relay()."""

    def __init__(self):
        self._inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[str] = []
        self.closed = False

    async def feed_text(self, data: str) -> None:
        await self._inbox.put(aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, data, None))

    async def feed_close(self) -> None:
        await self._inbox.put(aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, None, None))

    async def send_str(self, data: str) -> None:
        self.sent.append(data)

    async def close(self) -> None:
        self.closed = True
        await self._inbox.put(None)  # sentinel: end of stream

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._inbox.get()
        if item is None:
            raise StopAsyncIteration
        return item


class FakeConnector:
    def __init__(self, ws: FakeWS):
        self._ws = ws
        self.connected_url: str | None = None

    async def connect(self, url: str) -> FakeWS:
        self.connected_url = url
        return self._ws


async def test_pump_forwards_text_until_stream_ends():
    source = FakeWS()
    sink = FakeWS()
    await source.feed_text('{"opcode":"update"}')
    await source.close()

    await pump(source, sink)

    assert sink.sent == ['{"opcode":"update"}']


async def test_pump_stops_on_explicit_close_message_without_forwarding_after():
    source = FakeWS()
    sink = FakeWS()
    await source.feed_text("first")
    await source.feed_close()
    await source.feed_text("should-not-be-forwarded")

    await pump(source, sink)

    assert sink.sent == ["first"]


async def test_relay_forwards_both_directions_and_closes_peer_when_one_ends():
    ws_a = FakeWS()
    ws_b = FakeWS()
    await ws_a.feed_text("from-a")
    await ws_a.close()

    await relay(ws_a, ws_b)

    assert ws_b.sent == ["from-a"]
    assert ws_b.closed is True


async def test_relay_route_forwards_frames_end_to_end():
    room_relays = {"ABCD": "wss://ecast-relay-prod-01.jackboxgames.com/ws"}
    upstream_ws = FakeWS()
    await upstream_ws.feed_text('{"opcode":"welcome"}')
    connector = FakeConnector(upstream_ws)

    app = build_app()
    app.router.add_get("/ws", create_relay_handler(room_relays, connector))

    async with TestClient(TestServer(app)) as client:
        client_ws = await client.ws_connect("/ws?role=host&roomId=ABCD&token=xyz")

        msg = await asyncio.wait_for(client_ws.receive(), timeout=2)
        assert msg.type == aiohttp.WSMsgType.TEXT
        assert msg.data == '{"opcode":"welcome"}'

        await client_ws.send_str('{"opcode":"event"}')
        await asyncio.sleep(0.05)
        assert upstream_ws.sent == ['{"opcode":"event"}']

        await client_ws.close()
        await asyncio.sleep(0.05)

    assert (
        connector.connected_url
        == "wss://ecast-relay-prod-01.jackboxgames.com/ws?role=host&roomId=ABCD&token=xyz"
    )


async def test_relay_route_returns_404_for_unknown_room():
    connector = FakeConnector(FakeWS())
    app = build_app()
    app.router.add_get("/ws", create_relay_handler({}, connector))

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/ws", headers={"Connection": "Upgrade", "Upgrade": "websocket"})
        # No roomId in query -> handler responds 404 before attempting upgrade.
        assert resp.status == 404


async def test_register_relay_route_accepts_ws_upgrade_on_a_non_ws_path():
    """The real game's WS URL is built client-side from the rewritten "host"
    field (see rooms.py) - confirmed by live traffic and by a community
    server for the same game (johnbox) not checking path either. The route
    can't assume "/ws"; it must accept the upgrade wherever it lands.

    Uses a fresh Application, not build_app(): build_app() already registers
    the "*" browser-stub catch-all on this same "/{tail:.*}" pattern, and
    aiohttp forbids adding a specific-method route to a resource that already
    has an ANY route - the relay has to be registered on its own pattern
    first, exactly as factory.build_full_app() actually orders it."""
    room_relays = {"ABCD": "wss://ecast-relay-prod-01.jackboxgames.com/ws"}
    upstream_ws = FakeWS()
    await upstream_ws.feed_text('{"opcode":"welcome"}')
    connector = FakeConnector(upstream_ws)

    app = web.Application()
    register_relay_route(app, room_relays, connector)

    async with TestClient(TestServer(app)) as client:
        client_ws = await client.ws_connect("/some/other/path?role=host&roomId=ABCD")
        msg = await asyncio.wait_for(client_ws.receive(), timeout=2)
        assert msg.type == aiohttp.WSMsgType.TEXT
        assert msg.data == '{"opcode":"welcome"}'
        await client_ws.close()


async def test_register_relay_route_falls_back_for_non_websocket_requests():
    """A plain GET (no Upgrade header) anywhere must not be treated as a
    relay candidate, even with a known room - it's most likely a browser
    visiting the bridge directly."""
    connector = FakeConnector(FakeWS())
    seen = []

    async def fallback(request):
        seen.append(request.path)
        return web.Response(text="fallback reached")

    app = web.Application()
    register_relay_route(app, {"ABCD": "wss://x/ws"}, connector, fallback=fallback)

    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/some/random/path")
        assert resp.status == 200
        assert await resp.text() == "fallback reached"

    assert seen == ["/some/random/path"]


async def test_pump_logs_every_forwarded_frame(caplog):
    """The Ecast message catalog is being reverse-engineered from live
    traffic, and the relay is the only place real frames are visible."""
    import logging

    import aiohttp

    from bridgebox.server.relay import pump

    class Msg:
        def __init__(self, data):
            self.type = aiohttp.WSMsgType.TEXT
            self.data = data

    class Source:
        closed = False

        def __aiter__(self):
            async def gen():
                yield Msg('{"opcode":"client/welcome"}')
                yield Msg("y" * 2000)

            return gen()

    class Sink:
        closed = False

        def __init__(self):
            self.sent = []

        async def send_str(self, data):
            self.sent.append(data)

        async def close(self):
            self.closed = True

        def __aiter__(self):
            return self

    sink = Sink()
    with caplog.at_level(logging.DEBUG, logger="bridgebox.server.relay"):
        await pump(Source(), sink, label="ABCD game->upstream", level=logging.INFO)

    text = caplog.text
    assert '{"opcode":"client/welcome"}' in text
    assert "ABCD game->upstream" in text
    # Frames pass through byte-identical no matter how they were logged.
    assert sink.sent == ['{"opcode":"client/welcome"}', "y" * 2000]
    # ...but an oversized frame is truncated in the log, not dumped whole.
    assert "y" * 2000 not in text
    assert "+1200 chars" in text


async def test_pump_logs_no_frame_contents_unless_asked(caplog):
    """The default is silence, and this test used to assert the opposite.

    `level=logging.DEBUG` looked like "off" and was not: setup_logging hands
    the Logs screen every record regardless of the configured level, because
    the screen has its own DEBUG pill. So `log_frames: false` kept the game's
    protocol out of the FILE while streaming it into the UI - 1858 frame lines
    in one real session with the setting off the whole time."""

    class Msg:
        type = aiohttp.WSMsgType.TEXT

        def __init__(self, data):
            self.data = data

    class Source:
        def __aiter__(self):
            async def gen():
                yield Msg('{"opcode":"client/welcome","token":"secret"}')

            return gen()

    class Sink:
        closed = False

        def __init__(self):
            self.sent = []

        async def send_str(self, data):
            self.sent.append(data)

        async def close(self):
            self.closed = True

        def __aiter__(self):
            return self

    sink = Sink()
    with caplog.at_level(logging.DEBUG, logger="bridgebox.server.relay"):
        await pump(Source(), sink, label="ABCD game->upstream")

    assert "client/welcome" not in caplog.text
    assert "secret" not in caplog.text
    # The frame is still relayed - this is a logging change, not a routing one.
    assert sink.sent == ['{"opcode":"client/welcome","token":"secret"}']
    # And the summary still lands, so the session is not invisible.
    assert "ended: 1 frames" in caplog.text


# ---- WS close reason truncation ------------------------------------------


def test_close_reason_bytes_fits_the_real_upstream_rejection_message():
    """The message that was actually showing up truncated in the UI - now
    must survive whole."""
    msg = (
        "403, message='Invalid response status', "
        "url='wss://ecast-prod-use2.jackboxgames.com/ws?role=host&roomId=XNAR'"
    )
    result = _close_reason_bytes(msg)
    assert result == msg.encode("utf-8")


def test_close_reason_bytes_respects_the_rfc6455_control_frame_limit():
    result = _close_reason_bytes("x" * 500)
    assert len(result) == WS_CLOSE_REASON_MAX_BYTES


def test_close_reason_bytes_does_not_split_a_multibyte_character():
    """A cut that lands mid-character would produce bytes that don't decode
    as UTF-8 at all - the close frame must still be valid UTF-8."""
    result = _close_reason_bytes("ошибка: " + "я" * 100)
    assert len(result) <= WS_CLOSE_REASON_MAX_BYTES
    result.decode("utf-8")  # must not raise
