import logging

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from bridgebox.server.net_trace import build_trace_config


async def _make_app() -> web.Application:
    app = web.Application()

    async def handle(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/probe", handle)
    return app


async def test_a_fresh_request_logs_dns_and_new_connection(caplog):
    async with TestServer(await _make_app()) as server:
        session = aiohttp.ClientSession(trace_configs=[build_trace_config()])
        try:
            with caplog.at_level(logging.DEBUG, logger="bridgebox.server.net_trace"):
                async with session.get(server.make_url("/probe")) as resp:
                    assert resp.status == 200
        finally:
            await session.close()

    text = caplog.text
    assert "new connection established" in text
    assert "-> HTTP 200" in text


async def test_a_second_request_reuses_the_pooled_connection(caplog):
    async with TestServer(await _make_app()) as server:
        session = aiohttp.ClientSession(trace_configs=[build_trace_config()])
        try:
            async with session.get(server.make_url("/probe")):
                pass
            with caplog.at_level(logging.DEBUG, logger="bridgebox.server.net_trace"):
                async with session.get(server.make_url("/probe")):
                    pass
        finally:
            await session.close()

    assert "reused a pooled connection" in caplog.text


async def test_a_room_token_in_the_url_is_redacted(caplog):
    async def handle(request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/room", handle)

    async with TestServer(app) as server:
        session = aiohttp.ClientSession(trace_configs=[build_trace_config()])
        try:
            with caplog.at_level(logging.DEBUG, logger="bridgebox.server.net_trace"):
                async with session.get(
                    server.make_url("/room").with_query(token="super-secret")
                ):
                    pass
        finally:
            await session.close()

    # Scoped to this module's own records: aiohttp's built-in access log
    # (a different logger) logs the raw, unredacted URL by design - that is
    # not this module's redaction to fix, and asserting against the whole
    # caplog.text made this test order-dependent on whether that other
    # logger's records happened to propagate into it.
    our_records = " ".join(
        r.getMessage() for r in caplog.records if r.name == "bridgebox.server.net_trace"
    )
    assert our_records, "expected at least one net_trace log line"
    assert "super-secret" not in our_records
