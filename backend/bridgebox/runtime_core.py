from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path

import aiohttp
from aiohttp import web

from . import i18n
from .config import Config
from .diagnostics import STRATEGY_SETTLE_S
from .paths import resolve_project_path
from .server.app import build_ssl_context as _build_ssl_context
from .server.factory import BLOBCAST_SOCKETIO_APP
from .server.factory import build_full_app as _build_full_app
from .server.app import run_server as _run_server
from .server.net_trace import build_trace_config
from .server.relay import AiohttpWsConnector
from .server.rooms import AiohttpUpstreamClient
from .tls.ca import CA_CERT_FILENAME, CA_INSTALLED_MARKER
from .tls.ca import generate_leaf_cert as _generate_leaf_cert
from .tls.ca import install_ca_windows as _install_ca_windows
from .zapret.process import ZapretProcess, console_flags
from .zapret.strategies import discover_strategies as _discover_strategies
from .zapret.strategies import resolve_strategy as _resolve_strategy
from .zapret.strategies import resolve_zapret_layout

logger = logging.getLogger(__name__)


def _idle_status(config: Config, *, cert_installed: bool = False, notice: str | None = None) -> dict:
    return {
        "running": False,
        "host": config.server.host,
        "port": config.server.port,
        "zapretRunning": False,
        "zapretPid": None,
        "zapretError": None,
        "certInstalled": cert_installed,
        "zapretNotice": notice,
    }


# Shown to the user when the winws console is closed by hand. Russian, because
# it reaches the UI verbatim - the same rule the rest of this app's
# user-visible strings follow.
CONSOLE_CLOSED_NOTICE = "Консоль Zapret была закрыта пользователем. Мост остановлен."


def _default_session_factory() -> aiohttp.ClientSession:
    """The real default - wired here, not as a bare `aiohttp.ClientSession`
    class reference, so every test's `session_factory=...` fake keeps taking
    zero arguments. Only the production path gains the DNS/connect/reuse
    tracing; nothing about the DI surface changes."""
    return aiohttp.ClientSession(trace_configs=[build_trace_config()])


class RuntimeCore:
    """Pure async orchestration for the BridgeBox server + Zapret lifecycle.
    All I/O is injected (constructor defaults point at the real
    implementations, same DI style as ZapretProcess/RoomsProxy elsewhere in
    this codebase) so this is fully unit-testable without a real event-loop
    thread, real sockets, or a real subprocess - see BridgeRuntime for the
    thin thread/loop shim that actually drives this from pywebview's Api."""

    def __init__(
        self,
        *,
        config: Config,
        project_root: Path,
        zapret_process: ZapretProcess | None = None,
        session_factory=_default_session_factory,
        build_full_app=_build_full_app,
        run_server=_run_server,
        build_ssl_context=_build_ssl_context,
        generate_leaf_cert=_generate_leaf_cert,
        install_ca_windows=_install_ca_windows,
        upstream_client_factory=AiohttpUpstreamClient,
        ws_connector_factory=AiohttpWsConnector,
        discover_strategies=_discover_strategies,
        resolve_strategy=_resolve_strategy,
    ):
        self._config = config
        self._project_root = project_root
        self._zapret_process = zapret_process if zapret_process is not None else ZapretProcess()
        self._session_factory = session_factory
        self._build_full_app = build_full_app
        self._run_server = run_server
        self._build_ssl_context = build_ssl_context
        self._generate_leaf_cert = generate_leaf_cert
        self._install_ca_windows = install_ca_windows
        self._upstream_client_factory = upstream_client_factory
        self._ws_connector_factory = ws_connector_factory
        self._discover_strategies = discover_strategies
        self._resolve_strategy = resolve_strategy

        self._session: aiohttp.ClientSession | None = None
        self._http_client = None
        self._prewarm_task: asyncio.Task | None = None
        self._runner: web.AppRunner | None = None
        self._socketio_runner: web.AppRunner | None = None
        self._status: dict = _idle_status(config)
        # Set when winws died without being asked to (see _on_zapret_exit) and
        # carried in status() until the next start(), so the UI can say what
        # happened rather than just showing a bridge that turned itself off.
        self._zapret_notice: str | None = None
        # Wired by BridgeRuntime, which owns the loop this has to run on.
        self._zapret_exit_handler = None
        # start() and stop() both check a field, then await, then act on what
        # they checked - and several callers can reach them at once: the UI
        # toggle, the updater, the strategy suite, the zapret watchdog and the
        # app's own shutdown. Unguarded, two concurrent stops both saw a live
        # socket.io runner and both cleaned it up, which the log showed as
        # "blobcast socket.io listener stopped" three times in one teardown.
        # Created lazily: a Lock binds to the running loop, and this object is
        # constructed on the main thread before that loop exists.
        self._gate: asyncio.Lock | None = None

    def _lock(self) -> asyncio.Lock:
        if self._gate is None:
            self._gate = asyncio.Lock()
        return self._gate

    def status(self) -> dict:
        return dict(self._status)

    def set_zapret_exit_handler(self, handler) -> None:
        """Who to tell when winws exits on its own.

        Injected rather than constructed here because the reaction - tearing
        the bridge down - is an async call that has to reach the background
        loop, and this class deliberately owns no loop."""
        self._zapret_exit_handler = handler

    def _on_zapret_exit(self, code) -> None:
        """The watchdog's callback. Runs on the watchdog thread, so it does the
        least it can: record why, then hand the actual teardown to whoever
        owns the event loop."""
        self._zapret_notice = CONSOLE_CLOSED_NOTICE
        self._status = {**self._status, "zapretRunning": False, "zapretPid": None}
        logger.warning("zapret exited unexpectedly (code=%s) - stopping the bridge", code)
        handler = self._zapret_exit_handler
        if handler is not None:
            handler()

    def set_config(self, config: Config) -> None:
        """Swap in a newer Config (Api.update_config() calls this so a
        Settings change - strategy, port, hide_console - actually affects
        the *next* start() in this running session, not just config.yaml for
        the app's next launch). Has no effect on an already-running bridge;
        the caller must stop()/start() to apply it."""
        self._config = config

    @property
    def zapret_process(self) -> ZapretProcess:
        """Exposed so diagnostics (Settings' "Тест стратегий") can reuse the
        same ZapretProcess instead of spinning up a second winws.exe, which
        could otherwise conflict with the one this runtime may already be
        managing (WinDivert handles don't share cleanly across processes)."""
        return self._zapret_process

    def ensure_certificate(self):
        """Issue the leaf certificate and make sure the CA is trusted.

        Split out of start() so the first-run wizard's «Установить сертификат»
        can do exactly what a bridge start does, rather than a second
        implementation of the same four steps that could drift from it - the
        marker and the CA-vs-leaf distinction below are both things this repo
        has already gotten wrong once.

        Returns (cert_paths, cert_installed). Synchronous: it is filesystem
        work plus one certutil call, and both callers (start(), and
        Api.install_certificate()) want it finished before they continue.
        """
        cert_dir = resolve_project_path(self._project_root, self._config.server.tls.cert_dir)
        cert_dir.mkdir(parents=True, exist_ok=True)
        cert_paths = self._generate_leaf_cert(cert_dir)
        # The CA, not the leaf. This used to hand install_ca_windows
        # cert_paths.cert - the localhost leaf - so what actually landed in the
        # Trusted Root store was the server certificate itself, trusted
        # directly. It worked only because a self-trusted leaf needs no chain,
        # and it meant reissuing the leaf silently stopped being trusted, while
        # removing the old CA by its subject name could never match what was
        # really installed. generate_leaf_cert has already created the CA.
        ca_cert = cert_dir / CA_CERT_FILENAME

        marker = cert_dir / CA_INSTALLED_MARKER
        cert_installed = marker.exists()
        if not cert_installed:
            cert_installed = bool(self._install_ca_windows(ca_cert))
            if cert_installed:
                marker.write_text("installed")
            logger.info("CA install to Windows Trusted Root: %s", "ok" if cert_installed else "FAILED")
        else:
            logger.info("CA already installed (marker found at %s)", marker)
        return cert_paths, cert_installed

    async def start(self) -> dict:
        async with self._lock():
            return await self._start()

    async def _start(self) -> dict:
        if self._runner is not None:
            return self.status()

        # A new run answers the previous run's obituary.
        self._zapret_notice = None
        cert_paths, cert_installed = self.ensure_certificate()

        # The session is only published to self once the server is actually
        # listening. Binding fails routinely - the port is still held by a
        # previous instance, or another app owns it - and assigning it up
        # front meant every failed start leaked a ClientSession (with its
        # connector and sockets): stop() never ran because start() raised, and
        # the next attempt sailed past the `self._runner is not None` guard
        # and overwrote the reference. A user who retries the button five
        # times leaked five of them.
        session = self._session_factory()
        try:
            http_client = self._upstream_client_factory(session)
            ws_connector = self._ws_connector_factory(session)

            app = self._build_full_app(
                host=self._config.server.host,
                port=self._config.server.port,
                http_client=http_client,
                ws_connector=ws_connector,
                rewrite=self._config.rewrite,
                proxy_config=self._config.proxy,
                profiles=self._config.profiles,
                # A closure over self, not a resolved string: self._config is
                # replaced wholesale by update_config() (see desktop.py), so
                # reading it lazily here is what lets a language switch in
                # Settings reach the browser stub pages without a bridge
                # restart - see build_full_app's own comment on this param.
                lang=lambda: i18n.resolve_locale(self._config.ui.language),
            )
            ssl_context = self._build_ssl_context(cert_paths.cert, cert_paths.key)
            runner = await self._run_server(
                app, self._config.server.host, self._config.server.port, ssl_context=ssl_context
            )
            # Blobcast (Party Pack 1-6) needs a second site, because the
            # game's socket.io session goes to a port IT picks - 38203,
            # established by packet capture - not the one configured here.
            # Without this listener that half of the session silently goes
            # nowhere, which is precisely the failure that took several
            # diagnostic runs to see.
            #
            # Guarded by a getattr/get rather than assumed: build_full_app is
            # injected, and tests substitute a plain object for it.
            socketio_app = app.get(BLOBCAST_SOCKETIO_APP) if hasattr(app, "get") else None
            socketio_runner = None
            socketio_port = self._config.profiles.active("blobcast").blobcast.socketio_port
            if socketio_app is not None:
                socketio_runner = await self._run_server(
                    socketio_app,
                    self._config.server.host,
                    socketio_port,
                    ssl_context=ssl_context,
                )
        except BaseException:
            await session.close()
            raise

        self._session = session
        self._http_client = http_client
        self._runner = runner
        self._socketio_runner = socketio_runner
        if socketio_runner is not None:
            # The port actually bound, not the module constant - they differ the
            # moment the profile sets its own, and a log line that names the
            # wrong port is worse than none when the session will not connect.
            logger.info(
                "blobcast socket.io listening on https://%s:%s",
                self._config.server.host,
                socketio_port,
            )
        logger.info(
            "bridge listening on https://%s:%s", self._config.server.host, self._config.server.port
        )

        zapret_running = False
        zapret_pid = None
        zapret_error = None
        if self._config.zapret.enabled:
            try:
                zapret_dir = resolve_project_path(self._project_root, self._config.zapret.dir)
                layout = resolve_zapret_layout(zapret_dir)
                strategies = self._discover_strategies(layout.strategies_dir)
                strategy = self._resolve_strategy(self._config.zapret.strategy, strategies)
                hide_console = self._config.zapret.hide_console
                zapret_pid = self._zapret_process.start(
                    strategy.path,
                    creationflags=console_flags(hide_console),
                    on_exit=self._on_zapret_exit,
                    # Hidden console means its output would be lost; capture it
                    # into the log instead. A visible console keeps its output
                    # where the user asked for it.
                    capture_output=hide_console,
                )
                zapret_running = True
                logger.info("zapret started: strategy=%s pid=%s", strategy.key, zapret_pid)
            except Exception as exc:
                zapret_error = str(exc)
                logger.error("zapret failed to start: %s", exc)

        self._status = {
            "running": True,
            "host": self._config.server.host,
            "port": self._config.server.port,
            "zapretRunning": zapret_running,
            "zapretPid": zapret_pid,
            "zapretError": zapret_error,
            "certInstalled": cert_installed,
            "zapretNotice": None,
        }
        self._prewarm_task = asyncio.create_task(self._prewarm_upstream())
        return self.status()

    async def _prewarm_upstream(self) -> None:
        """Best-effort: open the TLS connection to both the active Ecast AND
        Blobcast upstreams while the user is still looking at "Мост запущен",
        so the keep-alive connection AiohttpUpstreamClient's session pools
        per host is already live by the time the game's first REST call
        (room creation, or Blobcast's own GET /room) makes its own request -
        the request that would otherwise pay a full TCP+TLS handshake on top
        of zapret's own fragmentation overhead right when the user is
        waiting on it. Idea borrowed from Flowseal's tg-ws-proxy, whose WS
        pool warms connections the same way before a client needs one (see
        CREDITS.md).

        Waits for WinDivert to actually be in the packet path first - the
        same settle time diagnostics.py's strategy suite uses. Firing
        earlier would just be a plain cold connect zapret never touched, no
        different from not pre-warming at all.

        Both upstreams in parallel, not sequentially: neither depends on the
        other, and halving the exposure window before the user might act
        costs nothing extra to write. Silent on failure by design: if either
        doesn't land in time, that game's own first request just does its
        normal cold connect, exactly as before this existed."""
        if self._config.zapret.enabled:
            await asyncio.sleep(STRATEGY_SETTLE_S)
        http_client = self._http_client
        if http_client is None:
            return
        upstreams = [self._config.profiles.active(kind).upstream for kind in ("ecast", "blobcast")]
        await asyncio.gather(*(self._prewarm_one(http_client, upstream) for upstream in upstreams))

    async def _prewarm_one(self, http_client, upstream: str) -> None:
        try:
            await http_client.request("GET", upstream, headers={}, data=None)
            logger.debug("upstream connection pre-warmed: %s", upstream)
        except Exception as exc:
            logger.debug("upstream pre-warm failed (harmless): %s", exc)

    async def stop(self) -> dict:
        async with self._lock():
            return await self._stop()

    async def _stop(self) -> dict:
        if self._prewarm_task is not None:
            self._prewarm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._prewarm_task
            self._prewarm_task = None

        if self._zapret_process.is_running:
            self._zapret_process.stop()
            logger.info("zapret stopped")

        if self._socketio_runner is not None:
            await self._socketio_runner.cleanup()
            self._socketio_runner = None
            logger.info("blobcast socket.io listener stopped")

        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            logger.info("bridge server stopped")

        if self._session is not None:
            await self._session.close()
            self._session = None
            self._http_client = None

        self._status = _idle_status(
            self._config,
            cert_installed=self._status.get("certInstalled", False),
            notice=self._zapret_notice,
        )
        return self.status()
