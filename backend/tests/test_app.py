import ssl
from pathlib import Path

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from bridgebox.server.app import build_app, build_ssl_context, run_server
from bridgebox.tls.ca import generate_leaf_cert


async def test_browser_stub_returns_warning_html():
    app = build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/")
        assert resp.status == 200
        assert resp.content_type == "text/html"
        body = await resp.text()
        assert "у моста нет веб-интерфейса" in body
        # Self-contained by necessity: this program exists for networks where
        # a request to a CDN is exactly what does not work, and the bridge
        # serves no static assets of its own.
        assert "http://" not in body and "https://" not in body


async def test_browser_stub_matches_arbitrary_nested_path():
    app = build_app()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/some/random/nested/path")
        assert resp.status == 200
        body = await resp.text()
        assert "BridgeBox" in body


def test_build_ssl_context_loads_localhost_cert(tmp_path: Path):
    leaf = generate_leaf_cert(tmp_path)

    ctx = build_ssl_context(leaf.cert, leaf.key)

    assert isinstance(ctx, ssl.SSLContext)


async def test_run_server_serves_stub_over_tls(tmp_path: Path):
    leaf = generate_leaf_cert(tmp_path)
    ssl_context = build_ssl_context(leaf.cert, leaf.key)

    app = build_app()
    runner = await run_server(app, "127.0.0.1", 0, ssl_context=ssl_context)

    # Port 0 means "pick any free port" - recover the one actually bound.
    port = runner.addresses[0][1]

    client_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_ctx.load_verify_locations(cafile=str(tmp_path / "bridgebox-ca.pem"))

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://127.0.0.1:{port}/", ssl=client_ctx
            ) as resp:
                assert resp.status == 200
                body = await resp.text()
                assert "BridgeBox" in body
    finally:
        await runner.cleanup()
