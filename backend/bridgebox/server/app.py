from __future__ import annotations

import logging
import ssl
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

from .pages import landing_page
from .rooms import redact


def wants_html(request: web.Request) -> bool:
    """Whether the caller is something that can actually render the warning
    page - i.e. a human's browser rather than the game.

    Accept, not User-Agent: it is the header that states what the caller can
    display, and the stub is only ever useful to something that displays
    HTML. A browser navigating here sends "text/html,application/xhtml+xml,
    ..."; the game is a libcurl client that sends "*/*" (confirmed from its
    own requests - "JackboxGames/1.00 libcurl/7.57.0-DEV ...").

    Everything else is forwarded upstream instead, which is what makes an
    endpoint we have never seen work anyway."""
    return "text/html" in request.headers.get("Accept", "")


async def handle_browser_request(request: web.Request, lang: str = "ru") -> web.Response:
    # In the full app this is only reached for a caller that asked for HTML
    # (see wants_html and factory.build_full_app) - a human who typed the
    # bridge address into a browser. Everything else is forwarded upstream,
    # because the game treats this bridge as its entire server and a path we
    # don't recognise is an endpoint we hadn't seen, not an error.
    # SECURITY FIX (H3): path_qs carries the query string.
    logger.info("browser request: %s %s", request.method, redact(request.path_qs))
    # Rendered per request rather than cached: it is a few lines of HTML, a
    # human sees it rarely, and a cached copy could only ever answer in
    # whatever language happened to be active when the module was imported -
    # the bug a `lang` parameter exists to avoid.
    return web.Response(text=landing_page(lang), content_type="text/html")


def register_browser_stub(app: web.Application) -> None:
    """Register the catch-all warning page. Must be registered LAST: it
    matches every path, so anything registered after it would be shadowed.

    Only build_app (the bare app, used by tests) uses this. The full app
    registers its own catch-all that forwards upstream and falls back to the
    warning page only for HTML callers - see factory.build_full_app."""
    app.router.add_route("*", "/{tail:.*}", handle_browser_request)


def build_app() -> web.Application:
    """Bare app with only the browser-warning catch-all - no upstream, so
    nothing to forward to. The real app is factory.build_full_app, which
    controls route ordering explicitly rather than relying on aiohttp's
    route-resolution order."""
    app = web.Application()
    register_browser_stub(app)
    return app


def build_ssl_context(cert_path: str | Path, key_path: str | Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    # Pinned rather than left to whatever the linked OpenSSL's security level
    # happens to permit. The game is the only client, it speaks TLS 1.2+, and
    # a floor that moves with the build is not a floor.
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


async def run_server(
    app: web.Application,
    host: str,
    port: int,
    *,
    ssl_context: ssl.SSLContext | None = None,
) -> web.AppRunner:
    """Start serving app on host:port and return the runner. Caller owns the
    runner's lifecycle and must call `await runner.cleanup()` to stop."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port, ssl_context=ssl_context)
    await site.start()
    return runner
