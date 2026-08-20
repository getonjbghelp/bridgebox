from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Callable

from aiohttp import web

from .app import handle_browser_request, wants_html
from .guard import MIDDLEWARES, is_top_level_navigation
from .pages import service_page

# The Ecast roots the game actually talks to, used only to decide which of the
# two browser pages to show. Mirrors EcastSettings.paths' default; it is a
# fallback for when the configured list is empty, which is the normal state
# when forward_all is on.
WELL_KNOWN_ECAST_PATHS = ("/api", "/tts", "/media")
from .blobcast import (
    BLOBCAST_UPSTREAM,
    BlobcastSessions,
    build_socketio_app,
    create_blobcast_handler,
    is_blobcast_path,
)
from .relay import WsConnector, register_relay_route
from .rooms import (
    UPSTREAM_BASE,
    RoomsProxy,
    UpstreamClient,
    create_api_handler,
    path_is_forwarded,
)

if TYPE_CHECKING:
    from ..config import ProfilesConfig, ProxyConfig, RewriteConfig

logger = logging.getLogger(__name__)

__all__ = ["UPSTREAM_BASE", "BLOBCAST_SOCKETIO_APP", "build_full_app"]

# Key under which build_full_app stashes the socket.io listener's app.
BLOBCAST_SOCKETIO_APP = web.AppKey("blobcast_socketio_app", web.Application)


def build_full_app(
    *,
    host: str,
    port: int,
    http_client: UpstreamClient,
    ws_connector: WsConnector,
    upstream_base: str | None = None,
    rewrite: "RewriteConfig | None" = None,
    proxy_config: "ProxyConfig | None" = None,
    profiles: "ProfilesConfig | None" = None,
    # A callable, not a resolved locale: every other lang= parameter in this
    # app (RuntimeCore, tray.py) reads config.ui.language fresh so a Settings
    # switch takes effect with no restart, and these two browser pages are
    # the one surface still reachable while the bridge is running - baking in
    # whatever was active when the bridge last started would have quietly
    # broken that promise for them alone.
    lang: Callable[[], str] = lambda: "ru",
) -> web.Application:
    """Compose the full BridgeBox app: the Ecast proxy and WS relay sharing
    one room_relays map, with a catch-all last.

    Route order is load-bearing. Both routes are wildcard-ish: the relay owns
    every GET path (its exact path isn't confirmed - see
    relay.register_relay_route), and the catch-all owns everything else. The
    catch-all has to come last or it would swallow the relay's WS handshakes.

    There is deliberately no separate /api route any more. Which paths get
    forwarded is now a user setting - the active Ecast profile's own
    forward_all/paths (ProxyConfig is the legacy, pre-profiles source, kept
    for callers and configs that predate profiles) - and a hardcoded /api
    route registered ahead of the catch-all would have quietly overridden it:
    removing "/api" from the list in Settings would have changed nothing.
    One route, one policy, so the setting means what it says. Blobcast is
    classified and forwarded before this gate is even consulted - see
    forward_or_warn - so narrowing Ecast's paths can never starve it.

    The catch-all forwards upstream rather than answering with the browser
    warning page. The game is pointed at this bridge as its entire server, so
    an unfamiliar path is not an error - it is an endpoint we hadn't seen, and
    the real server is the one that knows what to do with it. FixyText is the
    case that proved it: it POSTs its TTS job to /tts/generate, got the
    warning page back with HTTP 200, and read an HTML document where it
    expected generated speech.

    http_client/ws_connector are injected rather than constructed here because
    both need a live aiohttp.ClientSession, which must be created inside a
    running event loop - that belongs to whatever entry point actually starts
    the server, not to this synchronous route-registration step (aiohttp
    requires all routes registered before the app starts)."""
    # SECURITY FIX (H1/H2/M1). The bridge authenticates nothing and listens
    # where every page in the user's browser can reach it, so the one gate that
    # exists has to be impossible to route around - see server/guard.py. A
    # middleware rather than a check per handler: there are three entry points
    # (this catch-all, the WS relay route, and the socket.io site on its own
    # port), and a rule repeated three times is a rule the fourth will miss.
    app = web.Application(middlewares=list(MIDDLEWARES))
    room_relays: dict[str, str] = {}

    # Precedence, most explicit first: an upstream_base argument (tests
    # pointing at a local fake), then the active profile, then the legacy
    # rewrite field, then the constant. Profiles sit above rewrite rather
    # than replacing it because RewriteConfig.upstream_base still exists in
    # the schema - removing a config key silently resets it for everyone who
    # had set it, and Config migrates the value into a profile instead.
    if profiles is not None:
        ecast_profile = profiles.active("ecast")
        blobcast_profile = profiles.active("blobcast")
        if upstream_base is None:
            upstream_base = ecast_profile.upstream
        # Response rewriting belongs to the Ecast profile now. Rebuilt into a
        # RewriteConfig here rather than threaded through as a new type, so
        # RoomsProxy - which is also on the untouched Ecast path - keeps the
        # exact shape it already takes.
        if rewrite is None:
            from ..config import rewrite_for as _rewrite_for

            rewrite = _rewrite_for(ecast_profile)
        blobcast_upstream = blobcast_profile.upstream
        blobcast_settings = blobcast_profile.blobcast
    else:
        from ..config import BlobcastSettings as _BlobcastSettings

        if upstream_base is None:
            upstream_base = rewrite.upstream_base if rewrite else UPSTREAM_BASE
        blobcast_upstream = BLOBCAST_UPSTREAM
        blobcast_settings = _BlobcastSettings()

    blobcast_paths = tuple(blobcast_settings.paths)

    # Precedence mirrors upstream_base/rewrite above: the active Ecast
    # profile's own forward_all/paths win when profiles are in play, the
    # legacy proxy_config argument is the fallback for callers that predate
    # profiles (and the tests exercising that path directly), "forward
    # everything" is what is left if neither was ever given.
    if profiles is not None:
        forward_all = ecast_profile.ecast.forward_all
        allowed_paths = tuple(ecast_profile.ecast.paths)
    else:
        forward_all = proxy_config.forward_all if proxy_config else True
        allowed_paths = tuple(proxy_config.paths) if proxy_config else ()

    local_ws_base = f"wss://{host}:{port}/ws"
    proxy = RoomsProxy(
        upstream_base=upstream_base,
        local_ws_base=local_ws_base,
        http_client=http_client,
        room_relays=room_relays,
        rewrite=rewrite,
    )

    api_handler = create_api_handler(proxy)

    # Party Pack 1-6 and several singles speak Blobcast ("API v1"), which
    # lives on a completely different upstream and a disjoint set of paths -
    # /room, /accessToken, /socket.io/* against Ecast's /api/v2/*. Because
    # they never collide, one listener serves both with nothing to switch,
    # and a second proxy pointed at the other host is the whole mechanism.
    #
    # Before this, one upstream_base served everything: playing a Party Pack
    # 1-6 game meant hand-editing the setting to blobcast.jackboxgames.com,
    # which then broke every Party Pack 7+ game until it was edited back.
    # Blobcast forwards; it does not rewrite Ecast-shaped fields. Everything
    # off except the fallback User-Agent, which only ever fills in when the
    # game sent none - and a request without one is answered 403 by Jackbox's
    # load balancer (the finding rooms.FALLBACK_USER_AGENT documents).
    from ..config import RewriteConfig as _RewriteConfigForBlobcast

    blobcast_passthrough = _RewriteConfigForBlobcast(
        server_enabled=False,
        origin_enabled=False,
        user_agent_enabled=True,
    )

    blobcast_sessions = BlobcastSessions()
    blobcast_proxy = RoomsProxy(
        upstream_base=blobcast_upstream,
        local_ws_base=local_ws_base,
        http_client=http_client,
        room_relays=room_relays,
        # Emphatically NOT the Ecast profile's rewrite. Passing it here meant
        # the Ecast profile's Origin and User-Agent switches silently governed
        # Blobcast requests too - exactly the cross-profile bleed that moving
        # these settings into EcastSettings was meant to end. Blobcast's own
        # rewriting is create_blobcast_handler's job; this proxy only forwards.
        rewrite=blobcast_passthrough,
    )
    blobcast_handler = create_blobcast_handler(
        create_api_handler(blobcast_proxy),
        blobcast_sessions,
        # A bare hostname, never a host:port or a URL - BlobcastSettings
        # enforces that, because the game appends the port itself and anything
        # else becomes an unresolvable name. None means interception is off:
        # the field passes through and the session goes straight to Jackbox.
        local_host=(
            blobcast_settings.local_server_name
            if blobcast_settings.intercept_session
            else None
        ),
    )

    async def forward_or_warn(request: web.Request) -> web.StreamResponse:
        """Anything that isn't a WS handshake. A caller that can render HTML
        gets the warning page; a Blobcast-shaped request is always forwarded,
        unconditionally - its own paths list is its whole scope, and it must
        not depend on Ecast's forward_all/paths, which used to sit ahead of
        this split and could silently 404 Blobcast when narrowed. Everything
        else is Ecast, and gets forwarded if the current settings allow that
        path."""
        # SECURITY FIX (remote token leak via a mismatched pair of checks).
        # guard.refuse_browser_initiated exempts a request from the block
        # using Sec-Fetch-Mode/Dest (is_top_level_navigation); this branch
        # used to decide "give it a page instead of forwarding" using a
        # DIFFERENT test, Accept: text/html (wants_html). An <object>/<embed>
        # load is a real Sec-Fetch-Mode: navigate with Sec-Fetch-Dest: object
        # or embed (not "document"), and its Accept header is not required to
        # contain text/html - so it could pass the guard as a navigation and
        # still fall through every branch below to a real forward, exactly
        # like an ordinary blocked request. Checking is_top_level_navigation
        # here too guarantees this branch fires for every request the guard
        # ever exempts, so nothing exempted can reach the forwarding branches
        # further down. See test_h2_reopens_for_an_object_or_embed_style_navigation.
        if wants_html(request) or is_top_level_navigation(request):
            # Two pages, and the split is by path INSIDE this branch - never
            # ahead of it. A path check that ran first would answer the game
            # with HTML, which is exactly the /tts/generate bug documented at
            # the top of this file.
            # NOT "would this be forwarded": with forward_all on - the default -
            # that is every path, and it would swallow the landing page too.
            # The question is whether the path means something to the game.
            service = is_blobcast_path(request.path, blobcast_paths) or path_is_forwarded(
                request.path, allowed_paths or WELL_KNOWN_ECAST_PATHS
            )
            if service:
                # 403 rather than 200: this refuses to forward, and a browser
                # reload here is a real request to Jackbox under the game's
                # identity - see pages.service_page.
                logger.warning(
                    "browser opened the service path %s %s - refused, not forwarded",
                    request.method,
                    request.path,
                )
                return web.Response(
                    status=403, text=service_page(request.path, lang()), content_type="text/html"
                )
            return await handle_browser_request(request, lang())

        # A browser asks for this on every visit with `Accept: image/...`, so
        # it slips past wants_html and used to be FORWARDED to Jackbox, which
        # answers 403 - two real upstream round trips per idle browser visit.
        if request.path == "/favicon.ico":
            return web.Response(status=204)

        if is_blobcast_path(request.path, blobcast_paths):
            return await blobcast_handler(request)

        if forward_all or path_is_forwarded(request.path, allowed_paths):
            return await api_handler(request)

        # Refused loudly and in JSON. The old failure mode for a path the
        # bridge didn't handle was a silent HTML page with HTTP 200, which is
        # how /tts/generate broke without a single line in the log. Whoever
        # narrows this list should be told exactly what it just cost them.
        logger.warning(
            "not forwarding %s %s - it is outside the configured Ecast paths (%s). "
            "Add it in Settings, or turn on \"весь трафик\".",
            request.method,
            request.path,
            ", ".join(allowed_paths) or "<empty>",
        )
        body = json.dumps(
            {
                "ok": False,
                "error": f"path not proxied by BridgeBox: {request.path}",
            }
        ).encode("utf-8")
        return web.Response(status=404, body=body, content_type="application/json")

    register_relay_route(app, room_relays, ws_connector, fallback=forward_or_warn)
    app.router.add_route("*", "/{tail:.*}", forward_or_warn)

    # Stashed on the app rather than returned, so build_full_app keeps its
    # signature and every existing caller and test is untouched. The game's
    # socket.io session cannot share this site: it goes to a port the GAME
    # chooses (38203, established by packet capture), not the one the bridge
    # is configured with. Whoever starts the bridge starts this alongside it.
    # Absent when interception is off - runtime_core reads this to decide
    # whether to raise the second listener at all.
    if blobcast_settings.intercept_session:
        app[BLOBCAST_SOCKETIO_APP] = build_socketio_app(
            blobcast_sessions,
            http_client,
            ws_connector,
            port=blobcast_settings.socketio_port,
            log_frames=blobcast_settings.log_frames,
        )
    return app
