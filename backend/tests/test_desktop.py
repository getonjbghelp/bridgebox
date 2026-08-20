import json
import sys
import threading
import time
from pathlib import Path

import pytest

from bridgebox import desktop
from bridgebox import integrity
from bridgebox.config import Config
from bridgebox.log_buffer import LogBuffer
from bridgebox.window_chrome import THEMED_FULL


async def _fake_probe_targets(session, targets):
    """Stands in for diagnostics.probe_targets in _test_connection_coro
    tests - that function hits the real ecast.jackboxgames.com/ecast-prod-
    use2.jackboxgames.com over the actual internet, which a test must not
    depend on for pass/fail or network access."""
    return {name: {"ok": True, "elapsedMs": 1.0, "status": 200, "error": None} for name, _ in targets}


def test_is_admin_returns_bool_without_raising():
    result = desktop.is_admin()
    assert isinstance(result, bool)


def test_main_exits_when_not_admin(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["prog"])

    with pytest.raises(SystemExit) as exc_info:
        desktop.main(admin_check=lambda: False)

    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Administrator" in captured.err
    assert "run.bat" in captured.err


def test_main_never_creates_window_when_not_admin(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["prog"])
    called = {"create_window": False}

    def fake_create_window(*args, **kwargs):
        called["create_window"] = True

    monkeypatch.setattr(desktop.webview, "create_window", fake_create_window)
    monkeypatch.setattr(desktop.webview, "start", lambda: None)

    with pytest.raises(SystemExit):
        desktop.main(admin_check=lambda: False)

    assert called["create_window"] is False


def test_main_refuses_windows_7_before_even_checking_admin(monkeypatch, capsys):
    from bridgebox.platform_support import WindowsVersion

    monkeypatch.setattr(sys, "argv", ["prog"])
    admin_checked = []
    notices = []
    monkeypatch.setattr(
        desktop, "show_unsupported_notice", lambda version, **kw: notices.append(version)
    )

    with pytest.raises(SystemExit) as exc_info:
        desktop.main(
            admin_check=lambda: admin_checked.append(True) or True,
            windows_version=lambda: WindowsVersion(6, 1, 7601),  # Windows 7 SP1
        )

    # The refusal has to come first - clearing UAC only to be told the app
    # cannot run at all would be a pointless prompt.
    assert admin_checked == []
    assert "10 or newer" in str(exc_info.value)
    assert len(notices) == 1
    assert notices[0] == WindowsVersion(6, 1, 7601)


def test_main_refuses_windows_8_1(monkeypatch):
    from bridgebox.platform_support import WindowsVersion

    monkeypatch.setattr(sys, "argv", ["prog"])
    monkeypatch.setattr(desktop, "show_unsupported_notice", lambda *a, **k: None)

    with pytest.raises(SystemExit):
        desktop.main(admin_check=lambda: True, windows_version=lambda: WindowsVersion(6, 3, 9600))


def test_main_proceeds_on_windows_10(monkeypatch):
    from bridgebox.platform_support import WindowsVersion

    monkeypatch.setattr(sys, "argv", ["prog", "--dev"])
    fake_window = _FakeWindow()
    monkeypatch.setattr(desktop.webview, "create_window", lambda *a, **k: fake_window)
    monkeypatch.setattr(desktop.webview, "start", lambda: None)

    # Must not raise.
    desktop.main(admin_check=lambda: True, windows_version=lambda: WindowsVersion(10, 0, 19045))


def test_main_never_shows_the_unsupported_notice_on_a_supported_windows(monkeypatch):
    from bridgebox.platform_support import WindowsVersion

    monkeypatch.setattr(sys, "argv", ["prog", "--dev"])
    fake_window = _FakeWindow()
    monkeypatch.setattr(desktop.webview, "create_window", lambda *a, **k: fake_window)
    monkeypatch.setattr(desktop.webview, "start", lambda: None)
    calls = []
    monkeypatch.setattr(desktop, "show_unsupported_notice", lambda *a, **k: calls.append(a))

    desktop.main(admin_check=lambda: True, windows_version=lambda: WindowsVersion(10, 0, 22631))

    assert calls == []


class _FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class _FakeWindow:
    def __init__(self):
        # Fresh Event instances per window: class-level attributes would be
        # shared across every _FakeWindow in the run, so handlers registered
        # by one test would still be attached in the next.
        self.events = type("Events", (), {})()
        self.events.closing = _FakeEvent()
        self.events.shown = _FakeEvent()
        # pywebview fills this in once the window really exists; None here
        # is what the title bar code correctly treats as "nothing to paint".
        self.native = None
        self.destroyed = 0
        self.evaluated: list[str] = []

    def destroy(self):
        self.destroyed += 1

    def evaluate_js(self, script):
        self.evaluated.append(script)


def test_main_proceeds_when_admin(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["prog", "--dev"])
    created = {}
    fake_window = _FakeWindow()

    def fake_create_window(title, url, **kwargs):
        created["title"] = title
        created["url"] = url
        return fake_window

    monkeypatch.setattr(desktop.webview, "create_window", fake_create_window)
    monkeypatch.setattr(desktop.webview, "start", lambda: None)

    # Real startup checks wait 4s for GitHub - this test's on_shown call
    # below runs them for real (a real Api, no fakes), and without this the
    # sleep outlives the test and the background loop's teardown, leaving a
    # "Task was destroyed but it is pending" warning at interpreter exit.
    desktop.main(admin_check=lambda: True, startup_check_delay_s=0)

    assert created["title"] == "BridgeBox"
    assert created["url"] == desktop.DEV_SERVER_URL

    # Both reach through the HWND, which does not exist until the window is
    # shown - so these must be wired to `shown`, not run inline after
    # create_window() where they would silently do nothing. Two handlers: the
    # title bar, and the deferred startup work (which also takes the tray icon
    # back down, since the window it stands in for is on screen again).
    assert len(fake_window.events.shown.handlers) == 2
    # pywebview inspects each handler's signature: zero parameters means it is
    # called with no arguments. A handler that took any would be passed the
    # window instead and blow up here.
    import inspect

    for handler in fake_window.events.shown.handlers:
        assert len(inspect.signature(handler).parameters) == 0
        handler()  # no real window -> no-op, must not raise

    # main() started a real BridgeRuntime background thread - clean it up via
    # the handler main() registered on window.events.closing, same as a real
    # window-close would.
    for handler in fake_window.events.closing.handlers:
        handler()


# ---- Api ----------------------------------------------------------------


class FakeRuntime:
    def __init__(self, *, status=None):
        self._status = status or {"running": False, "host": "127.0.0.1", "port": 8443}
        self.start_calls = 0
        self.stop_calls = 0

    def start(self, timeout=20.0):
        self.start_calls += 1
        self._status = {**self._status, "running": True}
        return dict(self._status)

    def stop(self, timeout=10.0):
        self.stop_calls += 1
        self._status = {**self._status, "running": False}
        return dict(self._status)

    def get_status(self):
        return dict(self._status)

    def run(self, coro_factory, timeout=20.0):
        import asyncio

        return asyncio.run(coro_factory())

    def submit(self, coro_factory):
        """Run to completion immediately and hand back a settled Future, so
        job-based Api calls are deterministic in tests without a real loop."""
        import asyncio
        from concurrent.futures import Future

        future: Future = Future()
        try:
            future.set_result(asyncio.run(coro_factory()))
        except Exception as exc:  # noqa: BLE001 - mirrored onto the Future
            future.set_exception(exc)
        return future


class FakeRuntimeCore:
    def __init__(self, zapret_process=None, *, certificate=True):
        self.zapret_process = zapret_process
        self.set_config_calls = []
        # What ensure_certificate() should report, or an exception to raise.
        self._certificate = certificate
        self.ensure_certificate_calls = 0

    def set_config(self, config):
        self.set_config_calls.append(config)

    def ensure_certificate(self):
        self.ensure_certificate_calls += 1
        if isinstance(self._certificate, Exception):
            raise self._certificate
        return object(), self._certificate

    async def stop(self):
        """The updater takes the whole bridge down before replacing files -
        the sockets and the WinDivert filter are what keep the driver loaded."""
        self.stop_calls = getattr(self, "stop_calls", 0) + 1
        return {"running": False}


def _make_api(tmp_path: Path, *, config: Config | None = None, runtime=None, runtime_core=None):
    config = config or Config()
    runtime = runtime or FakeRuntime()
    return desktop.Api(
        runtime=runtime,
        runtime_core=runtime_core or FakeRuntimeCore(),
        config=config,
        config_path=tmp_path / "config.yaml",
        project_root=tmp_path,
        log_buffer=LogBuffer(),
        # Real startup checks wait STARTUP_NETWORK_CHECK_DELAY_S before
        # reaching GitHub - tests want the fake network call, not the wait.
        startup_check_delay_s=0,
    )


def test_api_bridge_start_returns_ok_dict(tmp_path: Path):
    runtime = FakeRuntime()
    api = _make_api(tmp_path, runtime=runtime)

    result = api.bridge_start()

    assert result["ok"] is True
    assert result["running"] is True
    assert runtime.start_calls == 1


def test_api_bridge_start_never_raises_returns_error_dict(tmp_path: Path):
    class FailingRuntime(FakeRuntime):
        def start(self, timeout=20.0):
            raise TimeoutError("loop wedged")

    api = _make_api(tmp_path, runtime=FailingRuntime())

    result = api.bridge_start()

    assert result["ok"] is False
    assert "loop wedged" in result["error"]


def test_api_bridge_stop_returns_ok_dict(tmp_path: Path):
    runtime = FakeRuntime()
    api = _make_api(tmp_path, runtime=runtime)

    result = api.bridge_stop()

    assert result["ok"] is True
    assert result["running"] is False
    assert runtime.stop_calls == 1


def test_api_bridge_status_reflects_runtime(tmp_path: Path):
    runtime = FakeRuntime(status={"running": True, "host": "127.0.0.1", "port": 8443})
    api = _make_api(tmp_path, runtime=runtime)

    result = api.bridge_status()

    assert result["ok"] is True
    assert result["running"] is True


def test_api_get_config_returns_serialized_config(tmp_path: Path):
    config = Config()
    config.server.port = 9001
    api = _make_api(tmp_path, config=config)

    result = api.get_config()

    assert result["ok"] is True
    assert result["config"]["server"]["port"] == 9001


def test_api_update_config_merges_validates_and_persists(tmp_path: Path):
    api = _make_api(tmp_path)

    result = api.update_config({"zapret": {"strategy": "alternative-11"}, "ui": {"theme": "dark"}})

    assert result["ok"] is True
    assert result["config"]["zapret"]["strategy"] == "alternative-11"
    assert result["config"]["ui"]["theme"] == "dark"
    assert result["config"]["server"]["port"] == 8443  # untouched field survives merge
    assert (tmp_path / "config.yaml").exists()


def test_api_update_config_propagates_to_runtime_core(tmp_path: Path):
    """A Settings change must affect the next bridge_start() within this
    session, not just get saved for the app's next launch."""
    runtime_core = FakeRuntimeCore()
    api = _make_api(tmp_path, runtime_core=runtime_core)

    api.update_config({"zapret": {"strategy": "alternative-11"}})

    assert len(runtime_core.set_config_calls) == 1
    assert runtime_core.set_config_calls[0].zapret.strategy == "alternative-11"


def test_api_update_config_invalid_value_returns_error_without_raising(tmp_path: Path):
    api = _make_api(tmp_path)

    result = api.update_config({"server": {"port": 999999}})

    assert result["ok"] is False
    assert result["error"]


def test_api_list_strategies_groups_real_bat_files(tmp_path: Path):
    strategies_dir = tmp_path / "zapret" / "strategies"
    strategies_dir.mkdir(parents=True)
    (strategies_dir / "General.bat").write_text("@echo off\n")
    (strategies_dir / "Alternative 1.bat").write_text("@echo off\n")
    config = Config()
    config.zapret.dir = "zapret"
    api = _make_api(tmp_path, config=config)

    result = api.list_strategies()

    assert result["ok"] is True
    assert [s["key"] for s in result["groups"]["Основная"]] == ["general"]
    assert [s["key"] for s in result["groups"]["Альтернативы"]] == ["alternative-1"]


def test_api_list_strategies_missing_dir_returns_error_not_raise(tmp_path: Path):
    config = Config()
    config.zapret.dir = "does-not-exist"
    api = _make_api(tmp_path, config=config)

    result = api.list_strategies()

    assert result["ok"] is True  # discover_strategies on a missing dir just finds nothing
    assert result["groups"]["Основная"] == []


def test_api_test_connection_when_bridge_not_running(tmp_path: Path):
    runtime = FakeRuntime(status={"running": False, "host": "127.0.0.1", "port": 8443})
    api = _make_api(tmp_path, runtime=runtime)

    result = api.test_connection()

    assert result["ok"] is False
    assert "не запущен" in result["error"]


async def test_api_test_connection_ping_failure_does_not_block_room_creation(
    tmp_path: Path, monkeypatch
):
    """The ping step is purely informational: if a target is unreachable
    (e.g. one DPI-blocked host but not the other), the room-creation round
    trip below it still runs and can succeed on its own - the two are
    independent signals, not a single pass/fail gate."""
    import json

    from bridgebox.server.app import build_ssl_context, run_server
    from bridgebox.server.factory import build_full_app
    from bridgebox.server.rooms import UpstreamResponse
    from bridgebox.tls.ca import generate_leaf_cert

    async def failing_probe(session, targets):
        return {
            name: {"ok": False, "elapsedMs": None, "status": None, "error": "blocked"}
            for name, _ in targets
        }

    monkeypatch.setattr(desktop, "probe_targets", failing_probe)

    class FakeUpstream:
        async def request(self, method, url, *, headers, data):
            body = json.dumps(
                {"roomid": "TEST", "server": "wss://ecast-relay-prod-01.jackboxgames.com/ws"}
            ).encode()
            return UpstreamResponse(status=200, headers={"Content-Type": "application/json"}, body=body)

    class FakeWs:
        closed = False

        async def send_str(self, data):
            pass

        async def close(self):
            self.closed = True

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class FakeWsConnector:
        async def connect(self, url):
            return FakeWs()

    port = 18446
    leaf = generate_leaf_cert(tmp_path)
    app = build_full_app(
        host="127.0.0.1", port=port, http_client=FakeUpstream(), ws_connector=FakeWsConnector()
    )
    ssl_context = build_ssl_context(leaf.cert, leaf.key)
    runner = await run_server(app, "127.0.0.1", port, ssl_context=ssl_context)

    try:
        runtime = FakeRuntime(status={"running": True, "host": "127.0.0.1", "port": port})
        api = _make_api(tmp_path, runtime=runtime)

        result = await api._test_connection_coro()

        assert result["ok"] is True, result
        assert any("пинг" in step and "ошибка" in step for step in result["steps"])
        assert any("создана" in step for step in result["steps"])
    finally:
        await runner.cleanup()


async def test_api_test_connection_full_round_trip_against_a_real_local_server(
    tmp_path: Path, monkeypatch
):
    """Runs the actual create-room + room-lookup round trip against a real
    aiohttp server bound on localhost with a real generated cert - only the
    Jackbox side (http_client) is faked. No WS relay connect: see
    _test_connection_coro's docstring for why that step was dropped."""
    import json

    monkeypatch.setattr(desktop, "probe_targets", _fake_probe_targets)

    from bridgebox.server.app import build_ssl_context, run_server
    from bridgebox.server.factory import build_full_app
    from bridgebox.server.rooms import UpstreamResponse
    from bridgebox.tls.ca import generate_leaf_cert

    seen_headers: list[dict] = []
    seen_bodies: list[bytes] = []
    seen_calls: list[tuple[str, str]] = []

    class FakeUpstream:
        async def request(self, method, url, *, headers, data):
            seen_headers.append(headers)
            seen_calls.append((method, url))
            if data is not None:
                seen_bodies.append(data)
            body = json.dumps(
                {"roomid": "TEST", "server": "wss://ecast-relay-prod-01.jackboxgames.com/ws"}
            ).encode()
            return UpstreamResponse(status=200, headers={"Content-Type": "application/json"}, body=body)

    class FakeWsConnector:
        async def connect(self, url):
            raise AssertionError("test_connection no longer opens a WS relay connection")

    # A fixed (not ephemeral/0) port: the rewritten "server" field bakes the
    # port in as a literal string, so it has to match what we actually bind.
    port = 18443
    leaf = generate_leaf_cert(tmp_path)
    app = build_full_app(
        host="127.0.0.1", port=port, http_client=FakeUpstream(), ws_connector=FakeWsConnector()
    )
    ssl_context = build_ssl_context(leaf.cert, leaf.key)
    runner = await run_server(app, "127.0.0.1", port, ssl_context=ssl_context)

    try:
        runtime = FakeRuntime(status={"running": True, "host": "127.0.0.1", "port": port})
        api = _make_api(tmp_path, runtime=runtime)

        result = await api._test_connection_coro()

        assert result["ok"] is True, result
        assert any("пинг ecast.jackboxgames.com" in step for step in result["steps"])
        assert any("пинг ecast-prod-use2.jackboxgames.com" in step for step in result["steps"])
        assert any("создана" in step for step in result["steps"])
        assert any("подтверждена" in step for step in result["steps"])

        # Verified against the live API: aiohttp's default "Python/x aiohttp/y"
        # User-Agent gets a 403 text/html page from the AWS load balancer,
        # whose body fails to parse as "Expecting value: line 1 column 1
        # (char 0)" - the exact error this test's real-world counterpart hit.
        # The proxy forwards a UA that is already present instead of
        # substituting its browser fallback, so test_connection must send one.
        user_agent = next(
            value for key, value in seen_headers[0].items() if key.lower() == "user-agent"
        )
        assert "Mozilla/5.0" in user_agent, f"non-browser UA would get a 403: {user_agent!r}"

        # Verified against the live API: room creation is rejected outright
        # without this field ("invalid parameters: missing required field
        # userId") - it was silently missing here until that check.
        create_body = json.loads(seen_bodies[0])
        assert create_body.get("userId"), "room creation would be rejected by the real API"

        # The PRD's "создание комнаты/разрушение комнаты" - only the create
        # half existed, so every click left a real room behind on Jackbox's
        # servers. Every diagnostic run must clean up after itself.
        assert ("DELETE", "https://ecast.jackboxgames.com/api/v2/rooms/TEST") in seen_calls
        assert any("закрыта" in step for step in result["steps"])
    finally:
        await runner.cleanup()


async def test_test_connection_still_succeeds_when_the_room_cannot_be_closed(
    tmp_path: Path, monkeypatch
):
    """Cleanup is best-effort by design: by the time it runs the check has
    already proved everything it set out to. A refusal must be reported with
    what the server actually said - a first live run answered 403 "bad token",
    which is a different problem from "unsupported" and has a different fix -
    and must never turn into a failed connection test."""
    import json

    monkeypatch.setattr(desktop, "probe_targets", _fake_probe_targets)

    from bridgebox.server.app import build_ssl_context, run_server
    from bridgebox.server.factory import build_full_app
    from bridgebox.server.rooms import UpstreamResponse
    from bridgebox.tls.ca import generate_leaf_cert

    delete_urls: list[str] = []

    class RefusesDelete:
        async def request(self, method, url, *, headers, data):
            if method == "DELETE":
                delete_urls.append(url)
                return UpstreamResponse(
                    status=403, headers={}, body=b'{"ok":false,"error":"bad token"}'
                )
            # Envelope-wrapped, like the live API: the token is nested, so a
            # flat lookup would miss it.
            body = json.dumps(
                {"ok": True, "body": {"code": "TEST", "token": "tok-123"}}
            ).encode()
            return UpstreamResponse(
                status=200, headers={"Content-Type": "application/json"}, body=body
            )

    class NoWs:
        async def connect(self, url):
            raise AssertionError("no WS expected")

    port = 18444
    leaf = generate_leaf_cert(tmp_path)
    app = build_full_app(
        host="127.0.0.1", port=port, http_client=RefusesDelete(), ws_connector=NoWs()
    )
    runner = await run_server(
        app, "127.0.0.1", port, ssl_context=build_ssl_context(leaf.cert, leaf.key)
    )

    try:
        api = _make_api(
            tmp_path, runtime=FakeRuntime(status={"running": True, "host": "127.0.0.1", "port": port})
        )

        result = await api._test_connection_coro()

        assert result["ok"] is True, result
        # The server's own words, not an invented explanation.
        assert any("bad token" in step for step in result["steps"])
        # The token was found despite being nested inside the envelope, and it
        # travels in the query string. Measured against the real API, not
        # assumed: Authorization: Bearer <token> and a bare Authorization:
        # <token> both answer 403 "bad token"; ?token= answers 200 "ok".
        assert delete_urls == ["https://ecast.jackboxgames.com/api/v2/rooms/TEST?token=tok-123"]
    finally:
        await runner.cleanup()


async def test_api_test_connection_full_round_trip_with_the_real_host_field_shape(
    tmp_path: Path, monkeypatch
):
    """Reproduces the actual production response, captured live: creating a
    room for a real app ("fourbage") returned

        {"ok": true, "body": {"host": "ecast-prod-use2...", "code": "MNAK",
                               "token": "670f3779..."}}

    - envelope-wrapped, room code under "code" not "roomid", and the relay
    address under a bare "host" (no scheme) instead of "server" (full
    ws(s)://.../ws URL). The old flat body.get("roomid")/body.get("server")
    lookup found neither and reported "ответ без комнаты/relay" on every
    real room creation, even though the room really was created and a relay
    address really was present - just under different names/shape. No WS
    relay connect here - see _test_connection_coro's docstring."""
    import json

    from bridgebox.server.app import build_ssl_context, run_server
    from bridgebox.server.factory import build_full_app
    from bridgebox.server.rooms import UpstreamResponse
    from bridgebox.tls.ca import generate_leaf_cert

    monkeypatch.setattr(desktop, "probe_targets", _fake_probe_targets)

    def fake_response(path):
        if path.rstrip("/").endswith("/MNAK"):
            # The room-lookup GET - shape doesn't matter here, just 200.
            body = json.dumps({"ok": True, "body": {"code": "MNAK"}}).encode()
        else:
            body = json.dumps(
                {
                    "ok": True,
                    "body": {
                        "host": "ecast-prod-use2.jackboxgames.com",
                        "code": "MNAK",
                        "token": "670f3779de7658e56fb5306e",
                    },
                }
            ).encode()
        return UpstreamResponse(status=200, headers={"Content-Type": "application/json"}, body=body)

    class FakeUpstream:
        async def request(self, method, url, *, headers, data):
            return fake_response(url)

    class FakeWsConnector:
        async def connect(self, url):
            raise AssertionError("test_connection no longer opens a WS relay connection")

    port = 18444
    leaf = generate_leaf_cert(tmp_path)
    app = build_full_app(
        host="127.0.0.1", port=port, http_client=FakeUpstream(), ws_connector=FakeWsConnector()
    )
    ssl_context = build_ssl_context(leaf.cert, leaf.key)
    runner = await run_server(app, "127.0.0.1", port, ssl_context=ssl_context)

    try:
        runtime = FakeRuntime(status={"running": True, "host": "127.0.0.1", "port": port})
        api = _make_api(tmp_path, runtime=runtime)

        result = await api._test_connection_coro()

        assert result["ok"] is True, result
        assert any("MNAK" in step for step in result["steps"]), result["steps"]
        assert any("создана" in step for step in result["steps"]), result["steps"]
        assert any("подтверждена" in step for step in result["steps"]), result["steps"]
    finally:
        await runner.cleanup()


async def test_api_test_connection_succeeds_with_no_relay_field_present(
    tmp_path: Path, monkeypatch
):
    """A room-creation response with a real room code but genuinely neither
    "server" nor "host" is not a failure: test_connection no longer opens a
    WS relay connection (see _test_connection_coro's docstring), so a
    missing relay field is just a note about this particular app, not
    something the room-creation/lookup checks depend on."""
    import json

    from bridgebox.server.app import build_ssl_context, run_server
    from bridgebox.server.factory import build_full_app
    from bridgebox.server.rooms import UpstreamResponse
    from bridgebox.tls.ca import generate_leaf_cert

    monkeypatch.setattr(desktop, "probe_targets", _fake_probe_targets)

    class FakeUpstream:
        async def request(self, method, url, *, headers, data):
            body = json.dumps({"roomid": "TEST", "note": "no relay field here"}).encode()
            return UpstreamResponse(status=200, headers={"Content-Type": "application/json"}, body=body)

    class FakeWsConnector:
        async def connect(self, url):
            raise AssertionError("test_connection no longer opens a WS relay connection")

    port = 18445
    leaf = generate_leaf_cert(tmp_path)
    app = build_full_app(
        host="127.0.0.1", port=port, http_client=FakeUpstream(), ws_connector=FakeWsConnector()
    )
    ssl_context = build_ssl_context(leaf.cert, leaf.key)
    runner = await run_server(app, "127.0.0.1", port, ssl_context=ssl_context)

    try:
        runtime = FakeRuntime(status={"running": True, "host": "127.0.0.1", "port": port})
        api = _make_api(tmp_path, runtime=runtime)

        result = await api._test_connection_coro()

        assert result["ok"] is True, result
        create_step = next(s for s in result["steps"] if "создана" in s)
        assert "relay" not in create_step  # no relay field found -> no relay note
        assert any("подтверждена" in step for step in result["steps"]), result["steps"]
    finally:
        await runner.cleanup()


def test_api_test_strategies_with_no_strategies_on_disk(tmp_path: Path):
    config = Config()
    config.zapret.dir = "zapret"  # no strategies/ dir created
    api = _make_api(tmp_path, config=config)

    result = api.test_strategies()

    assert result["ok"] is True
    assert result["total"] == 0

    progress = api.test_strategies_progress()
    assert progress["ok"] is True
    assert progress["done"] is True
    assert progress["results"] == []
    assert progress["fastestKey"] is None


def test_api_test_strategies_progress_before_any_run_is_done_and_empty(tmp_path: Path):
    api = _make_api(tmp_path)

    progress = api.test_strategies_progress()

    assert progress["done"] is True
    assert progress["results"] == []
    assert progress["error"] is None


def test_api_test_strategies_cancel_is_safe_with_nothing_running(tmp_path: Path):
    api = _make_api(tmp_path)

    assert api.test_strategies_cancel()["ok"] is True


def test_api_test_strategies_refuses_a_second_concurrent_run(tmp_path: Path):
    api = _make_api(tmp_path)

    class _PendingFuture:
        def done(self):
            return False

    api._strategy_future = _PendingFuture()

    result = api.test_strategies()

    assert result["ok"] is False
    assert "уже выполняется" in result["error"]


def test_api_test_strategies_rejects_an_unknown_target_set(tmp_path: Path):
    api = _make_api(tmp_path)

    result = api.test_strategies(target_set="jackbox")

    assert result["ok"] is False
    assert "jackbox" in result["error"]
    assert result["total"] == 0


class _FakeZapretProcess:
    """Just enough of ZapretProcess for build_switch's switch() to run: an
    is_running flag it can stop, and a start() that records what it was
    asked to launch. Without something here, zapret_process is None and
    every switch() call fails before probe_targets is ever reached."""

    def __init__(self):
        self.is_running = False
        self.started_with: list = []

    def start(self, path, **kwargs):
        self.is_running = True
        self.started_with.append(path)

    def stop(self):
        self.is_running = False


def test_api_test_strategies_both_runs_two_full_stages(tmp_path: Path):
    """"both" is two complete passes, not one pass against four combined
    targets - so with one strategy on disk, "both" produces two result rows
    (ecast then blobcast), each carrying only its own two targets."""
    strategies_dir = tmp_path / "zapret" / "strategies"
    strategies_dir.mkdir(parents=True)
    (strategies_dir / "General.bat").write_text("@echo off\n")
    config = Config()
    config.zapret.dir = "zapret"
    api = _make_api(
        tmp_path, config=config, runtime_core=FakeRuntimeCore(zapret_process=_FakeZapretProcess())
    )

    started = api.test_strategies(target_set="both")
    assert started["ok"] is True, started
    assert started["total"] == 2  # 1 strategy x 2 stages

    progress = api.test_strategies_progress()
    assert progress["done"] is True, progress
    stages = [r["targetSet"] for r in progress["results"]]
    assert stages == ["ecast", "blobcast"]
    from bridgebox.diagnostics import BLOBCAST_TARGETS, ECAST_TARGETS

    assert set(progress["results"][0]["targets"]) == {n for n, _ in ECAST_TARGETS}
    assert set(progress["results"][1]["targets"]) == {n for n, _ in BLOBCAST_TARGETS}
    # The run finished, so nothing is "currently" in progress any more.
    assert progress["stage"] is None


def test_api_test_strategies_skip_heavy_excludes_the_heavy_group(tmp_path: Path):
    """The pre-filter lives here, not in run_strategy_suite - this is the
    only place skip_heavy=True is ever actually honoured."""
    strategies_dir = tmp_path / "zapret" / "strategies"
    strategies_dir.mkdir(parents=True)
    (strategies_dir / "General.bat").write_text("@echo off\n")
    (strategies_dir / "Fake TLS Auto.bat").write_text("@echo off\n")
    config = Config()
    config.zapret.dir = "zapret"
    api = _make_api(
        tmp_path, config=config, runtime_core=FakeRuntimeCore(zapret_process=_FakeZapretProcess())
    )

    started = api.test_strategies(skip_heavy=True)
    assert started["ok"] is True, started
    assert started["total"] == 1  # only General - "Прочие" was filtered out

    progress = api.test_strategies_progress()
    assert [r["key"] for r in progress["results"]] == ["general"]


def test_api_test_strategies_blobcast_only_probes_blobcast_targets(tmp_path: Path):
    strategies_dir = tmp_path / "zapret" / "strategies"
    strategies_dir.mkdir(parents=True)
    (strategies_dir / "General.bat").write_text("@echo off\n")
    config = Config()
    config.zapret.dir = "zapret"
    api = _make_api(
        tmp_path, config=config, runtime_core=FakeRuntimeCore(zapret_process=_FakeZapretProcess())
    )

    api.test_strategies(target_set="blobcast")
    progress = api.test_strategies_progress()

    from bridgebox.diagnostics import BLOBCAST_TARGETS

    assert len(progress["results"]) == 1
    assert progress["results"][0]["targetSet"] == "blobcast"
    assert set(progress["results"][0]["targets"]) == {n for n, _ in BLOBCAST_TARGETS}


def test_export_strategy_results_without_a_window_answers_instead_of_raising(tmp_path: Path):
    api = _make_api(tmp_path)
    api._strategy_results = [{"key": "k", "name": "n", "ok": True, "targets": {}, "error": None}]

    result = api.export_strategy_results("json")

    assert result["ok"] is False
    assert result["error"]


def test_export_strategy_results_refuses_an_unknown_format(tmp_path: Path):
    api = _make_api(tmp_path)

    result = api.export_strategy_results("xml")

    assert result["ok"] is False
    assert "xml" in result["error"]


def test_export_strategy_results_refuses_when_nothing_has_run(tmp_path: Path):
    class FakeWindow:
        def create_file_dialog(self, dialog_type, **kwargs):
            raise AssertionError("must not prompt for a save location with no results")

    api = _make_api(tmp_path)
    api.attach_window(FakeWindow())

    result = api.export_strategy_results("json")

    assert result["ok"] is False
    assert "нет результатов" in result["error"]


def test_export_strategy_results_writes_the_chosen_file(tmp_path: Path):
    import json

    dest = tmp_path / "out.json"

    class FakeWindow:
        def create_file_dialog(self, dialog_type, **kwargs):
            return [str(dest)]

    api = _make_api(tmp_path)
    api.attach_window(FakeWindow())
    api._strategy_results = [
        {"key": "general", "name": "General", "ok": True, "targetSet": "ecast", "targets": {}, "error": None}
    ]

    result = api.export_strategy_results("json")

    assert result["ok"] is True, result
    assert result["path"] == str(dest)
    saved = json.loads(dest.read_text(encoding="utf-8"))
    assert saved["results"][0]["key"] == "general"


def test_export_strategy_results_html_writes_a_report(tmp_path: Path):
    dest = tmp_path / "out.html"

    class FakeWindow:
        def create_file_dialog(self, dialog_type, **kwargs):
            return [str(dest)]

    api = _make_api(tmp_path)
    api.attach_window(FakeWindow())
    api._strategy_results = [
        {"key": "general", "name": "General", "ok": True, "targetSet": "ecast", "targets": {}, "error": None}
    ]

    result = api.export_strategy_results("html")

    assert result["ok"] is True, result
    assert dest.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_export_strategy_results_cancelled_dialog_is_not_an_error(tmp_path: Path):
    class CancelledDialog:
        def create_file_dialog(self, dialog_type, **kwargs):
            return None

    api = _make_api(tmp_path)
    api.attach_window(CancelledDialog())
    api._strategy_results = [
        {"key": "general", "name": "General", "ok": True, "targetSet": "ecast", "targets": {}, "error": None}
    ]

    result = api.export_strategy_results("json")

    assert result["ok"] is True
    assert result["path"] == ""


# ---- "Проверять при запуске" ---------------------------------------------


def test_startup_update_check_never_started_reports_not_started(tmp_path: Path):
    api = _make_api(tmp_path)

    result = api.startup_update_check()

    assert result["started"] is False
    assert result["done"] is False


def test_start_startup_update_check_does_nothing_when_the_setting_is_off(tmp_path: Path):
    config = Config()
    assert config.update.check_on_startup is False  # the documented default
    api = _make_api(tmp_path, config=config)

    api.start_startup_update_check()

    assert api.startup_update_check()["started"] is False


def test_start_startup_update_check_runs_when_the_setting_is_on(tmp_path: Path, monkeypatch):
    """This is the bug report itself: check_on_startup existed, had a UI
    toggle, and main() never once called the method that acts on it - the
    check simply never ran, regardless of the setting's value. Regression
    guard: turning it on must actually produce a result."""
    monkeypatch.setattr(desktop, "probe_targets", _fake_probe_targets)
    config = Config()
    config.update.check_on_startup = True
    api = _make_api(tmp_path, config=config)

    async def fake_check_update_coro():
        return {
            "ok": True, "error": None, "installed": "1.9.0", "latest": "1.10.0", "updateAvailable": True,
        }

    monkeypatch.setattr(api, "_check_update_coro", fake_check_update_coro)

    api.start_startup_update_check()
    result = api.startup_update_check()

    assert result["started"] is True
    assert result["done"] is True
    assert result["ok"] is True
    assert result["updateAvailable"] is True
    assert result["latest"] == "1.10.0"


def test_start_startup_update_check_waits_the_configured_delay_first(tmp_path: Path, monkeypatch):
    """STARTUP_NETWORK_CHECK_DELAY_S exists so a machine autostarting BridgeBox
    at Windows logon does not have this join every other logon program's rush
    at GitHub the instant the window paints. Regression guard: the delay is
    actually awaited, with the configured value, before the network call."""
    config = Config()
    config.update.check_on_startup = True
    api = _make_api(tmp_path, config=config)
    api._startup_check_delay_s = 7.5

    calls = []

    async def fake_sleep(seconds):
        calls.append(seconds)

    async def fake_check_update_coro():
        # The delay must happen BEFORE the network call, not after or
        # alongside it - otherwise a slow GitHub round trip could still land
        # during the logon rush this delay exists to dodge.
        assert calls == [7.5], "the network check ran before (or without) the startup delay"
        return {"ok": True, "error": None, "installed": "1.0.0", "latest": "1.0.0", "updateAvailable": False}

    monkeypatch.setattr(desktop.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(api, "_check_update_coro", fake_check_update_coro)

    api.start_startup_update_check()
    result = api.startup_update_check()

    assert calls == [7.5]
    assert result["done"] is True
    assert result["ok"] is True


def test_startup_update_check_survives_a_network_failure(tmp_path: Path):
    config = Config()
    config.update.check_on_startup = True

    class FailingRuntime(FakeRuntime):
        def submit(self, coro_factory):
            from concurrent.futures import Future

            future: Future = Future()
            future.set_exception(OSError("dns failure"))
            return future

    api = _make_api(tmp_path, config=config, runtime=FailingRuntime())

    api.start_startup_update_check()
    result = api.startup_update_check()

    assert result["started"] is True
    assert result["done"] is True
    assert result["ok"] is False
    assert "dns failure" in result["error"]


# ---- BridgeBox's own update check -----------------------------------------


def test_app_update_check_never_started_reports_not_started(tmp_path: Path):
    api = _make_api(tmp_path)

    result = api.app_update_check()

    assert result["started"] is False
    assert result["done"] is False
    assert result["dismissedVersion"] == ""


def test_start_app_update_check_does_nothing_when_the_setting_is_off(tmp_path: Path):
    config = Config()
    config.app_update.check_on_startup = False
    api = _make_api(tmp_path, config=config)

    api.start_app_update_check()

    assert api.app_update_check()["started"] is False


def test_app_update_check_is_on_by_default(tmp_path: Path):
    """Unlike zapret's UpdateConfig, this one defaults ON - see
    AppUpdateConfig's docstring for why (it is how a critical fix reaches
    somebody who never opens Settings)."""
    assert Config().app_update.check_on_startup is True


def test_start_app_update_check_runs_when_the_setting_is_on(tmp_path: Path, monkeypatch):
    api = _make_api(tmp_path)  # check_on_startup defaults to True

    async def fake_check_app_update_coro():
        return {
            "ok": True, "error": None, "installed": "0.1.2", "latest": "0.1.3",
            "notes": "- Fixed a glitch", "htmlUrl": "https://github.com/getonjbghelp/bridgebox/releases/tag/v0.1.3",
            "critical": False, "updateAvailable": True,
        }

    monkeypatch.setattr(api, "_check_app_update_coro", fake_check_app_update_coro)

    api.start_app_update_check()
    result = api.app_update_check()

    assert result["started"] is True
    assert result["done"] is True
    assert result["ok"] is True
    assert result["updateAvailable"] is True
    assert result["latest"] == "0.1.3"
    assert result["critical"] is False


def test_start_app_update_check_waits_the_configured_delay_first(tmp_path: Path, monkeypatch):
    api = _make_api(tmp_path)
    api._startup_check_delay_s = 3.0
    calls = []

    async def fake_sleep(seconds):
        calls.append(seconds)

    async def fake_check_app_update_coro():
        assert calls == [3.0], "the network check ran before (or without) the startup delay"
        return {
            "ok": True, "error": None, "installed": "0.1.2", "latest": "0.1.2",
            "notes": "", "htmlUrl": None, "critical": False, "updateAvailable": False,
        }

    monkeypatch.setattr(desktop.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(api, "_check_app_update_coro", fake_check_app_update_coro)

    api.start_app_update_check()

    assert calls == [3.0]


def test_a_second_start_app_update_check_does_not_pile_onto_a_live_run(tmp_path: Path):
    """`shown` fires again on every restore from the tray - a second call
    while one is in flight must not start a second network request."""
    api = _make_api(tmp_path)

    class NeverDoneFuture:
        def done(self):
            return False

    api._app_update_future = NeverDoneFuture()
    calls = {"n": 0}

    def spy_submit(coro_factory):
        calls["n"] += 1
        return NeverDoneFuture()

    api._runtime.submit = spy_submit
    api.start_app_update_check()

    assert calls["n"] == 0


def test_check_app_update_is_the_synchronous_manual_variant(tmp_path: Path, monkeypatch):
    """"Проверить сейчас" - blocking, no startup delay, distinct future from
    the automatic one."""
    api = _make_api(tmp_path)

    async def fake_check_app_update_coro():
        return {
            "ok": True, "error": None, "installed": "0.1.2", "latest": "0.1.4",
            "notes": "notes", "htmlUrl": "https://github.com/getonjbghelp/bridgebox/releases/tag/v0.1.4",
            "critical": True, "updateAvailable": True,
        }

    monkeypatch.setattr(api, "_check_app_update_coro", fake_check_app_update_coro)

    result = api.check_app_update()

    assert result["ok"] is True
    assert result["critical"] is True
    assert result["latest"] == "0.1.4"


def test_check_app_update_never_raises_returns_error_dict(tmp_path: Path):
    class FailingRuntime(FakeRuntime):
        def run(self, coro_factory, timeout=25.0):
            raise OSError("no network")

    api = _make_api(tmp_path, runtime=FailingRuntime())

    result = api.check_app_update()

    assert result["ok"] is False
    assert "no network" in result["error"]
    assert result["updateAvailable"] is False


def test_dismiss_app_update_persists_the_version_and_is_readable_back(tmp_path: Path):
    api = _make_api(tmp_path)

    result = api.dismiss_app_update("0.1.3")

    assert result["ok"] is True
    assert api._config.app_update.dismissed_version == "0.1.3"
    assert api.app_update_check()["dismissedVersion"] == "0.1.3"


def test_check_app_update_coro_reads_the_installed_version_from_version_module(
    tmp_path: Path, monkeypatch
):
    """installed must be BridgeBox's own version - not zapret's, not a copy
    kept anywhere else, so it can never drift from what version.app_version()
    (the single source of truth - see version.py) reports."""
    monkeypatch.setattr(desktop, "app_version", lambda: "9.9.9")

    async def failing_fetch(session, **kw):
        # Fails fast so this only checks what `installed` was recorded as,
        # not a real network round trip - aiohttp.ClientSession() itself is
        # still real (same as _check_update_coro's own design), just never
        # asked to make a request.
        raise OSError("no network")

    monkeypatch.setattr(desktop.app_update, "fetch_latest_release", failing_fetch)
    api = _make_api(tmp_path)

    import asyncio as _asyncio

    result = _asyncio.run(api._check_app_update_coro())

    assert result["installed"] == "9.9.9"


# ---- BridgeBox's own self-update (download + swap the running .exe) ------


def test_apply_app_update_coro_errors_out_when_not_frozen(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(desktop.app_update, "running_exe_path", lambda: None)
    api = _make_api(tmp_path)

    import asyncio as _asyncio

    result = _asyncio.run(api._apply_app_update_coro())

    assert result["ok"] is False
    assert result["version"] is None


def test_apply_app_update_coro_downloads_and_swaps_the_exe(tmp_path: Path, monkeypatch):
    exe_path = tmp_path / "BridgeBox.exe"
    exe_path.write_bytes(b"old")
    monkeypatch.setattr(desktop.app_update, "running_exe_path", lambda: exe_path)

    class _Release:
        version = "0.2.0"
        exe_url = "https://objects.githubusercontent.com/BridgeBox.exe"
        exe_digest = None  # nothing to verify - see verify_exe_digest's own tests

    async def fake_fetch(session, **kw):
        return _Release()

    downloaded = {}

    async def fake_download_exe(session, url, dest, **kw):
        downloaded["url"] = url
        downloaded["dest"] = dest
        dest.write_bytes(b"new")
        return dest

    swapped = {}

    def fake_replace(new_path, current_path):
        swapped["new_path"] = new_path
        swapped["current_path"] = current_path
        current_path.write_bytes(new_path.read_bytes())
        return current_path.with_name(current_path.name + ".old")

    manifest_calls = []
    monkeypatch.setattr(desktop.app_update, "fetch_latest_release", fake_fetch)
    monkeypatch.setattr(desktop.app_update, "download_exe", fake_download_exe)
    monkeypatch.setattr(desktop.app_update, "replace_running_exe", fake_replace)
    monkeypatch.setattr(desktop.integrity, "write_manifest", lambda root: manifest_calls.append(root))
    api = _make_api(tmp_path)

    import asyncio as _asyncio

    result = _asyncio.run(api._apply_app_update_coro())

    assert result == {"ok": True, "error": None, "version": "0.2.0"}
    assert downloaded["url"] == "https://objects.githubusercontent.com/BridgeBox.exe"
    assert swapped["current_path"] == exe_path
    assert exe_path.read_bytes() == b"new"
    # The exe on disk just changed out from under integrity.py's own
    # baseline - without a fresh manifest the very next launch would show
    # "files were modified" over a change this process just made itself.
    assert manifest_calls == [tmp_path]
    assert api._integrity.verified is True


def test_apply_app_update_coro_refuses_a_digest_mismatch_and_does_not_swap(
    tmp_path: Path, monkeypatch
):
    exe_path = tmp_path / "BridgeBox.exe"
    exe_path.write_bytes(b"old")
    monkeypatch.setattr(desktop.app_update, "running_exe_path", lambda: exe_path)

    class _Release:
        version = "0.2.0"
        exe_url = "https://objects.githubusercontent.com/BridgeBox.exe"
        exe_digest = "sha256:" + "a" * 64  # will never match b"new"

    async def fake_fetch(session, **kw):
        return _Release()

    async def fake_download_exe(session, url, dest, **kw):
        dest.write_bytes(b"new")
        return dest

    replace_calls = []

    def fake_replace(new_path, current_path):
        replace_calls.append((new_path, current_path))
        raise AssertionError("must not swap in an exe that failed its checksum")

    monkeypatch.setattr(desktop.app_update, "fetch_latest_release", fake_fetch)
    monkeypatch.setattr(desktop.app_update, "download_exe", fake_download_exe)
    monkeypatch.setattr(desktop.app_update, "replace_running_exe", fake_replace)
    api = _make_api(tmp_path)

    import asyncio as _asyncio

    result = _asyncio.run(api._apply_app_update_coro())

    assert result["ok"] is False
    assert replace_calls == []
    assert exe_path.read_bytes() == b"old"
    stage_path = tmp_path / "BridgeBox.exe.new"
    assert not stage_path.exists(), "a checksum-rejected download must not linger on disk"


def test_apply_app_update_coro_errors_when_the_release_has_no_exe_asset(
    tmp_path: Path, monkeypatch
):
    exe_path = tmp_path / "BridgeBox.exe"
    exe_path.write_bytes(b"old")
    monkeypatch.setattr(desktop.app_update, "running_exe_path", lambda: exe_path)

    class _Release:
        version = "0.2.0"
        exe_url = None

    async def fake_fetch(session, **kw):
        return _Release()

    monkeypatch.setattr(desktop.app_update, "fetch_latest_release", fake_fetch)
    api = _make_api(tmp_path)

    import asyncio as _asyncio

    result = _asyncio.run(api._apply_app_update_coro())

    assert result["ok"] is False
    assert exe_path.read_bytes() == b"old", "must not touch the exe if there is nothing to apply"


def test_apply_app_update_coro_never_touches_config(tmp_path: Path, monkeypatch):
    """The whole point: a self-update replaces one exe file next to itself
    and nothing else - config.yaml is a completely separate file this
    function never opens."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("server:\n  port: 12345\n", encoding="utf-8")
    exe_path = tmp_path / "BridgeBox.exe"
    exe_path.write_bytes(b"old")
    monkeypatch.setattr(desktop.app_update, "running_exe_path", lambda: exe_path)

    class _Release:
        version = "0.2.0"
        exe_url = "https://objects.githubusercontent.com/BridgeBox.exe"
        exe_digest = None

    async def fake_fetch(session, **kw):
        return _Release()

    async def fake_download_exe(session, url, dest, **kw):
        dest.write_bytes(b"new")
        return dest

    def fake_replace(new_path, current_path):
        current_path.write_bytes(new_path.read_bytes())
        return current_path.with_name(current_path.name + ".old")

    monkeypatch.setattr(desktop.app_update, "fetch_latest_release", fake_fetch)
    monkeypatch.setattr(desktop.app_update, "download_exe", fake_download_exe)
    monkeypatch.setattr(desktop.app_update, "replace_running_exe", fake_replace)
    api = _make_api(tmp_path)

    import asyncio as _asyncio

    _asyncio.run(api._apply_app_update_coro())

    assert config_path.read_text(encoding="utf-8") == "server:\n  port: 12345\n"


def test_app_apply_progress_when_never_started(tmp_path: Path):
    api = _make_api(tmp_path)

    result = api.app_apply_progress()

    assert result == {"started": False, "done": False, "ok": None, "error": None, "version": None}


def test_start_app_apply_update_then_app_apply_progress_reports_done(
    tmp_path: Path, monkeypatch
):
    async def fake_coro():
        return {"ok": True, "error": None, "version": "0.2.0"}

    api = _make_api(tmp_path)
    monkeypatch.setattr(api, "_apply_app_update_coro", fake_coro)

    api.start_app_apply_update()
    result = api.app_apply_progress()

    assert result == {"started": True, "done": True, "ok": True, "error": None, "version": "0.2.0"}


def test_a_second_start_app_apply_update_does_not_pile_onto_a_live_run(tmp_path: Path):
    api = _make_api(tmp_path)

    class NeverDoneFuture:
        def done(self):
            return False

    api._app_apply_future = NeverDoneFuture()
    calls = {"n": 0}

    def spy_submit(coro_factory):
        calls["n"] += 1
        return NeverDoneFuture()

    api._runtime.submit = spy_submit
    api.start_app_apply_update()

    assert calls["n"] == 0


def test_api_get_log_lines_delegates_to_log_buffer(tmp_path: Path):
    import json

    log_buffer = LogBuffer()
    log_buffer.append(json.dumps({"time": 1.0, "level": "info", "logger": "x", "message": "hi"}))
    api = desktop.Api(
        runtime=FakeRuntime(),
        runtime_core=FakeRuntimeCore(),
        config=Config(),
        config_path=tmp_path / "config.yaml",
        project_root=tmp_path,
        log_buffer=log_buffer,
    )

    result = api.get_log_lines()

    assert result["ok"] is True
    assert [line["message"] for line in result["lines"]] == ["hi"]


def _hostlist_path(tmp_path: Path) -> Path:
    return tmp_path / "zapret" / "lists" / "list-jackbox.txt"


def test_api_get_hostlist_returns_empty_text_when_the_file_is_missing(tmp_path: Path):
    api = _make_api(tmp_path)

    result = api.get_hostlist()

    assert result["ok"] is True
    assert result["text"] == ""


def test_api_save_then_get_hostlist_round_trips(tmp_path: Path):
    path = _hostlist_path(tmp_path)
    path.parent.mkdir(parents=True)
    api = _make_api(tmp_path)

    saved = api.save_hostlist("# hosts\necast.jackboxgames.com\njackbox.tv\n")

    assert saved["ok"] is True
    assert saved["count"] == 2
    assert api.get_hostlist()["text"] == "# hosts\necast.jackboxgames.com\njackbox.tv\n"


def test_api_save_hostlist_re_records_the_integrity_baseline(tmp_path: Path):
    """zapret/lists/*.txt is a watched path - saving through the app is not
    tampering, so the next integrity check must not flag it (same rule the
    zapret updater already follows)."""
    path = _hostlist_path(tmp_path)
    path.parent.mkdir(parents=True)
    api = _make_api(tmp_path)
    integrity.write_manifest(tmp_path)  # baseline taken before the edit

    result = api.save_hostlist("ecast.jackboxgames.com\n")

    assert result["ok"] is True
    report = integrity.verify(tmp_path)
    assert report.verified is True
    assert api.integrity_status()["verified"] is True


def test_api_save_hostlist_reports_the_offending_line_instead_of_raising(tmp_path: Path):
    path = _hostlist_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("ecast.jackboxgames.com\n", encoding="utf-8")
    api = _make_api(tmp_path)

    result = api.save_hostlist("ecast.jackboxgames.com\nhttps://oops\n")

    assert result["ok"] is False
    assert "строка 2" in result["error"]
    # The previous list must survive a rejected edit - winws reads it at the
    # next bridge start regardless of what the UI is showing.
    assert path.read_text(encoding="utf-8") == "ecast.jackboxgames.com\n"


# ---- temp dir and update ---------------------------------------------------


def test_temp_dir_reports_what_the_setting_actually_resolves_to(tmp_path: Path):
    """"temp" alone tells the user nothing about where their disk is used."""
    api = _make_api(tmp_path)

    result = api.get_temp_dir()

    assert result["ok"] is True
    assert result["path"] == "temp"
    assert result["resolved"].endswith("temp")
    assert Path(result["resolved"]).is_absolute()


def test_empty_temp_dir_falls_back_to_the_system_temp(tmp_path: Path):
    import tempfile

    config = Config()
    config.paths.temp_dir = ""
    api = _make_api(tmp_path, config=config)

    resolved = api.get_temp_dir()["resolved"]

    assert resolved.startswith(tempfile.gettempdir())


def test_pick_temp_dir_without_a_window_answers_instead_of_raising(tmp_path: Path):
    """Every Api method owes the frontend the same dict shape - dev mode and
    tests have no window."""
    api = _make_api(tmp_path)

    result = api.pick_temp_dir()

    assert result["ok"] is False
    assert result["error"]


def test_pick_temp_dir_saves_the_chosen_folder(tmp_path: Path):
    chosen = tmp_path / "downloads"
    chosen.mkdir()

    class FakeWindow:
        def create_file_dialog(self, dialog_type):
            return [str(chosen)]

    api = _make_api(tmp_path)
    api.attach_window(FakeWindow())

    result = api.pick_temp_dir()

    assert result["ok"] is True, result
    assert api.get_temp_dir()["resolved"] == str(chosen)


def test_cancelling_the_folder_picker_keeps_the_current_setting(tmp_path: Path):
    class CancelledDialog:
        def create_file_dialog(self, dialog_type):
            return None

    api = _make_api(tmp_path)
    api.attach_window(CancelledDialog())

    assert api.pick_temp_dir()["ok"] is True
    assert api.get_temp_dir()["path"] == "temp"


def test_update_progress_is_safe_before_any_run(tmp_path: Path):
    api = _make_api(tmp_path)

    result = api.zapret_update_progress()

    assert result["done"] is True
    assert result["phase"] == "idle"


def test_check_update_returns_the_standard_shape_on_a_network_failure(tmp_path: Path):
    """GitHub is routinely unreachable from the networks this app exists for,
    so the failure path is the common one, not the exotic one."""
    class FailingRuntime(FakeRuntime):
        def run(self, coro_factory, timeout=20.0):
            raise OSError("dns failure")

    api = _make_api(tmp_path, runtime=FailingRuntime())

    result = api.check_zapret_update()

    assert result["ok"] is False
    assert "dns failure" in result["error"]
    assert result["updateAvailable"] is False


# ---- carrying profiles between machines ----------------------------------


def _api_with_profiles(tmp_path):
    """A real Api over a temp config, so the export/import methods are
    exercised the way the UI calls them - a missing import in desktop.py is a
    NameError at runtime that no schema-level test would catch. (It was
    missing, and this is the test that found it.)"""
    from bridgebox.config import ProfilesConfig

    config = Config()
    config.profiles = ProfilesConfig(
        items=list(ProfilesConfig().items)
        + [{"id": "mine", "name": "Mine", "kind": "blobcast", "upstream": "https://mine.example"}]
    )
    return _make_api(tmp_path, config=config)


def test_api_exports_only_the_custom_profiles(tmp_path):
    api = _api_with_profiles(tmp_path)

    result = api.export_profiles()

    assert result["ok"] and result["count"] == 1
    assert "mine.example" in result["json"]
    assert "official-ecast" not in result["json"]


def test_api_import_round_trips_and_never_duplicates_the_builtins(tmp_path):
    api = _api_with_profiles(tmp_path)
    exported = api.export_profiles()["json"]

    result = api.import_profiles(exported)

    assert result["ok"], result["error"]
    assert result["report"]["added"] == 1
    ids = [p["id"] for p in result["config"]["profiles"]["items"]]
    assert ids.count("official-ecast") == 1
    assert len(ids) == len(set(ids)), "import must not create duplicate ids"


def test_api_import_reports_junk_instead_of_raising(tmp_path):
    api = _api_with_profiles(tmp_path)

    result = api.import_profiles("this is not json")

    assert result["ok"] is False
    assert result["error"]


def test_a_room_token_never_reaches_a_diagnostic_step():
    """Steps are shown in the диагностика popup and routinely pasted into bug
    reports. The room-creation response carries "token" - the credential that
    controls the room - and the parsed body was interpolated into these
    strings raw, bypassing the redaction that already covers the log."""
    body = {
        "ok": True,
        "body": {"token": "s3cr3t-room-token", "accessToken": "another", "apptag": "fourbage"},
    }

    rendered = desktop._redacted_json(body)

    assert "s3cr3t-room-token" not in rendered
    assert "another" not in rendered
    assert "<hidden>" in rendered
    # Still readable: the keys stay, which is what makes the line worth having.
    assert "token" in rendered
    assert "fourbage" in rendered


# ---- native title bar ------------------------------------------------------


def test_apply_window_theme_is_safe_before_a_window_exists(tmp_path: Path):
    """Dev mode and tests have no window; every Api method still owes a
    plain answer instead of an AttributeError."""
    api = _make_api(tmp_path)

    assert api.apply_window_theme() is False


def test_changing_the_theme_repaints_the_title_bar(tmp_path: Path, monkeypatch):
    """The regression this guards: the title bar is drawn by Windows and
    knows nothing about tokens.css, so toggling the theme in Settings left
    it on the old colour until the app was restarted."""
    applied: list[str] = []

    def fake_apply(window, theme):
        applied.append(theme)
        return THEMED_FULL

    monkeypatch.setattr(desktop, "apply_titlebar_theme", fake_apply)

    api = _make_api(tmp_path)
    api.attach_window(object())

    api.update_config({"ui": {"theme": "dark"}})

    assert applied == ["dark"]


def test_a_factory_reset_puts_the_title_bar_back_too(tmp_path: Path, monkeypatch):
    """Reset goes through the same update_config path, so the title bar has
    to follow the theme back to its default rather than keeping the colour
    of a theme that is no longer set."""
    applied: list[str] = []
    monkeypatch.setattr(
        desktop, "apply_titlebar_theme", lambda window, theme: applied.append(theme) or THEMED_FULL
    )

    config = Config()
    config.ui.theme = "light"
    api = _make_api(tmp_path, config=config)
    api.attach_window(object())

    api.update_config({"ui": None})  # null-unset: pydantic refills the default

    assert api._config.ui.theme == "dark"
    assert applied == ["dark"]


def test_a_failing_title_bar_repaint_never_fails_the_config_write(tmp_path: Path, monkeypatch):
    """Cosmetics must not be able to reject a setting the user just changed."""

    def exploding(window, theme):
        raise OSError("dwmapi refused")

    monkeypatch.setattr(desktop, "apply_titlebar_theme", exploding)

    api = _make_api(tmp_path)
    api.attach_window(object())

    result = api.update_config({"ui": {"theme": "dark"}})

    assert result["ok"] is False
    assert api._config.ui.theme == "dark", "the config write itself must still have landed"


# ---- first-run setup ----------------------------------------------------


def test_install_certificate_reports_success(tmp_path: Path):
    core = FakeRuntimeCore(certificate=True)
    api = _make_api(tmp_path, runtime_core=core)

    result = api.install_certificate()

    assert result == {"ok": True, "error": None, "certInstalled": True}
    assert core.ensure_certificate_calls == 1


def test_install_certificate_reports_a_refused_install_without_raising(tmp_path: Path):
    """certutil failing is a returncode, not an exception - the wizard's
    mandatory step has to be able to tell the difference between 'installed'
    and 'ran and did nothing', or its Next button unlocks on a lie."""
    api = _make_api(tmp_path, runtime_core=FakeRuntimeCore(certificate=False))

    result = api.install_certificate()

    assert result["ok"] is False
    assert result["certInstalled"] is False
    assert result["error"]


def test_install_certificate_survives_an_exploding_core(tmp_path: Path):
    api = _make_api(tmp_path, runtime_core=FakeRuntimeCore(certificate=OSError("certs/ read-only")))

    result = api.install_certificate()

    assert result["ok"] is False
    assert result["certInstalled"] is False
    assert "read-only" in result["error"]


def test_install_certificate_does_not_start_the_bridge(tmp_path: Path):
    """The whole point of splitting this out of bridge_start: a button that
    says it installs a certificate must not also bind ports and launch winws."""
    runtime = FakeRuntime()
    api = _make_api(tmp_path, runtime=runtime, runtime_core=FakeRuntimeCore())

    api.install_certificate()

    assert runtime.start_calls == 0


def test_setup_complete_starts_false_and_is_written_through_update_config(tmp_path: Path):
    api = _make_api(tmp_path)
    assert api.get_config()["config"]["ui"]["setup_complete"] is False

    result = api.update_config({"ui": {"setup_complete": True}})

    assert result["ok"] is True
    assert result["config"]["ui"]["setup_complete"] is True
    # Persisted, not just held in memory - the next launch reads the file.
    from bridgebox.config import load_config

    assert load_config(tmp_path / "config.yaml").ui.setup_complete is True


def test_a_factory_reset_brings_the_wizard_back(tmp_path: Path):
    """`ui: null` is how every reset button unsets a section, so the wizard
    gate riding in `ui` is what makes "после сброса настроек" work without a
    second mechanism."""
    config = Config()
    config.ui.setup_complete = True
    api = _make_api(tmp_path, config=config)

    api.update_config({"ui": None})

    assert api._config.ui.setup_complete is False


def test_two_config_writes_from_different_threads_do_not_lose_each_other(
    tmp_path: Path, monkeypatch
):
    """pywebview runs every JS call on its own thread, so two settings changed
    at once genuinely arrive concurrently. Both used to read the same starting
    config and the slower writer stomped the faster one's field - which is how
    a finished setup wizard could persist as setup_complete: false while the
    write itself reported ok.

    The sleep is what makes this deterministic rather than a flaky race: it
    holds the window open long enough that an unlocked version loses a field
    every run, not one run in fifty.
    """
    import threading
    import time

    from bridgebox.config import load_config

    api = _make_api(tmp_path)
    real_save = desktop.save_config

    def slow_save(config, path):
        time.sleep(0.05)
        real_save(config, path)

    monkeypatch.setattr(desktop, "save_config", slow_save)

    start = threading.Barrier(2)

    def write(patch):
        start.wait()
        api.update_config(patch)

    threads = [
        threading.Thread(target=write, args=({"ui": {"setup_complete": True}},)),
        threading.Thread(target=write, args=({"ui": {"sidebar_collapsed": True}},)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    saved = load_config(tmp_path / "config.yaml")
    assert saved.ui.setup_complete is True, "the wizard's completion was overwritten"
    assert saved.ui.sidebar_collapsed is True, "the sidebar state was overwritten"


async def test_update_sweeps_a_winws_this_session_never_started(tmp_path: Path, monkeypatch):
    """`is_running` only means "this session called start()". A winws left over
    from a previous BridgeBox run - or one the user launched by hand - is
    invisible to it, and it holds WinDivert64.sys the whole time. That is the
    reported "[WinError 5] Отказано в доступе" on the .sys, so the sweep has to
    happen unconditionally, before apply_update touches anything."""
    from bridgebox.zapret import update as zapret_update

    order: list[str] = []

    class _StoppedZapret:
        is_running = False

        def stop(self):  # pragma: no cover - must never be reached here
            order.append("stop")

    monkeypatch.setattr(desktop, "kill_all_winws", lambda: order.append("kill_all_winws"))
    # Waiting for the process to actually die is the step that stops the file
    # replacement from starting too early - pinned here, faked for speed.
    monkeypatch.setattr(
        desktop, "wait_for_winws_exit", lambda: order.append("wait_for_winws_exit") or True
    )
    monkeypatch.setattr(
        zapret_update,
        "fetch_latest_release",
        _async_result(zapret_update.Release(version="1.10.1", zip_url="", zip_size=1)),
    )
    monkeypatch.setattr(zapret_update, "download_archive", _async_result(tmp_path / "r.zip"))
    monkeypatch.setattr(
        zapret_update,
        "install_release",
        lambda *a, **k: order.append("install_release")
        or ([], zapret_update.StrategyPlan({}, [], [], [], [])),
    )

    api = _make_api(tmp_path, runtime_core=FakeRuntimeCore(zapret_process=_StoppedZapret()))
    await api._update_coro()

    assert order == ["kill_all_winws", "wait_for_winws_exit", "install_release"], (
        "sweep, then WAIT for the process to be gone, and only then replace files - "
        "taskkill returns once it has asked, not once winws has died"
    )
    assert api._runtime_core.stop_calls == 1, "the whole bridge goes down first"


def _async_result(value):
    async def _coro(*args, **kwargs):
        return value

    return _coro


# ---- autostart / tray / bridge-on-launch --------------------------------


def test_set_autostart_records_the_minimized_choice_the_task_cannot_report(
    tmp_path: Path, monkeypatch
):
    """schtasks can say whether a task exists, but not whether it carries
    --minimized. That half of the answer only lives in config.yaml."""
    monkeypatch.setattr(desktop, "enable_autostart", lambda *, minimized: True)
    monkeypatch.setattr(desktop, "autostart_is_enabled", lambda: True)
    api = _make_api(tmp_path)

    result = api.set_autostart(True, True)

    assert result["ok"] is True
    assert api._config.ui.autostart is True
    assert api._config.ui.autostart_minimized is True


def test_a_refused_autostart_is_not_recorded_as_enabled(tmp_path: Path, monkeypatch):
    """A toggle that shows "on" while Windows has no task is worse than an
    error: the user finds out at the next boot, or never."""
    monkeypatch.setattr(desktop, "enable_autostart", lambda *, minimized: False)
    monkeypatch.setattr(desktop, "autostart_is_enabled", lambda: False)
    api = _make_api(tmp_path)

    result = api.set_autostart(True, False)

    assert result["ok"] is False
    assert result["error"]
    assert api._config.ui.autostart is False


def test_get_autostart_trusts_windows_over_the_config_file(tmp_path: Path, monkeypatch):
    """Somebody can delete the task in Task Scheduler. The truth is the task."""
    config = Config()
    config.ui.autostart = True
    monkeypatch.setattr(desktop, "autostart_is_enabled", lambda: False)
    api = _make_api(tmp_path, config=config)

    assert api.get_autostart()["enabled"] is False


def test_close_hides_to_tray_only_while_the_tray_actually_exists(tmp_path: Path):
    """Returning False from `closing` cancels the close. If the tray failed to
    install, doing that would leave a window nobody can get rid of."""
    config = Config()
    config.ui.minimize_to_tray = True
    api = _make_api(tmp_path, config=config)

    assert api.wants_tray_on_close() is True

    config.ui.minimize_to_tray = False
    api.update_config({"ui": {"minimize_to_tray": False}})
    # Read at close time, not captured at startup, so the Settings toggle
    # applies to the very next close rather than after a restart.
    assert api.wants_tray_on_close() is False


def test_start_bridge_on_launch_does_nothing_when_switched_off(tmp_path: Path):
    runtime = FakeRuntime()
    api = _make_api(tmp_path, runtime=runtime)

    api.start_bridge_on_launch()

    assert runtime.start_calls == 0


def test_start_bridge_on_launch_never_blocks_the_caller(tmp_path: Path):
    """It runs on the window thread at startup. bridge_start blocks on a port
    bind and a winws spawn for up to 20s - doing that inline is a first paint
    the user watches hang."""
    import threading

    config = Config()
    config.ui.start_bridge_on_launch = True

    released = threading.Event()

    class SlowRuntime(FakeRuntime):
        def start(self, timeout=20.0):
            released.wait(2.0)
            return super().start(timeout)

    runtime = SlowRuntime()
    api = _make_api(tmp_path, config=config, runtime=runtime)

    api.start_bridge_on_launch()  # must return immediately, not in 2s
    assert runtime.start_calls == 0, "the caller was blocked by the bridge start"

    released.set()
    for _ in range(200):
        if runtime.start_calls:
            break
        import time

        time.sleep(0.01)
    assert runtime.start_calls == 1, "the bridge still has to actually start"


def test_start_bridge_on_launch_is_safe_to_call_again_on_every_tray_restore(tmp_path: Path):
    """It hangs off `shown`, which fires again each time the window comes back
    from the tray. A second start against a live bridge would race a port bind."""
    config = Config()
    config.ui.start_bridge_on_launch = True
    runtime = FakeRuntime(status={"running": True, "host": "127.0.0.1", "port": 8443})
    api = _make_api(tmp_path, config=config, runtime=runtime)

    api.start_bridge_on_launch()

    assert runtime.start_calls == 0


# ---- exporting the log ----------------------------------------------------


def _api_with_logs(tmp_path: Path, target: Path):
    """An Api whose save dialog always answers `target`, holding two log lines
    - one of them an exception, since the stack is the part an export exists
    for."""
    buffer = LogBuffer()
    buffer.append(
        json.dumps({"time": 1.0, "level": "info", "logger": "bridgebox", "message": "мост запущен"})
    )
    buffer.append(
        json.dumps(
            {
                "time": 2.0,
                "level": "error",
                "logger": "bridgebox",
                "message": "boom",
                "traceback": "Traceback...\nValueError: boom",
            }
        )
    )

    class FakeWindow:
        def create_file_dialog(self, dialog_type, **kwargs):
            return [str(target)]

    api = desktop.Api(
        runtime=FakeRuntime(),
        runtime_core=FakeRuntimeCore(),
        config=Config(),
        config_path=tmp_path / "config.yaml",
        project_root=tmp_path,
        log_buffer=buffer,
    )
    api.attach_window(FakeWindow())
    return api


def test_every_log_format_writes_a_file_with_the_stack_in_it(tmp_path: Path):
    for fmt in ("log", "json", "html"):
        target = tmp_path / f"logs.{fmt}"
        api = _api_with_logs(tmp_path, target)

        result = api.export_logs(fmt)

        assert result["ok"] is True, (fmt, result["error"])
        assert "ValueError: boom" in target.read_text(encoding="utf-8"), fmt


def test_exporting_an_unknown_format_is_refused(tmp_path: Path):
    api = _api_with_logs(tmp_path, tmp_path / "logs.pdf")

    result = api.export_logs("pdf")

    assert result["ok"] is False
    assert "pdf" in result["error"]


def test_a_cancelled_save_dialog_is_not_an_error(tmp_path: Path):
    """Backing out of a file dialog is a decision, not a failure - reporting it
    in red is how a UI teaches people to distrust its errors."""

    class Cancelled:
        def create_file_dialog(self, dialog_type, **kwargs):
            return None

    api = _api_with_logs(tmp_path, tmp_path / "unused.log")
    api.attach_window(Cancelled())

    result = api.export_logs("log")

    assert result["ok"] is True
    assert result["path"] == ""


def test_exporting_an_empty_log_says_so(tmp_path: Path):
    """Writing a zero-byte file and reporting success sends an empty bug
    report, which is worse than refusing."""

    class FakeWindow:
        def create_file_dialog(self, dialog_type, **kwargs):
            raise AssertionError("the dialog must not open with nothing to save")

    api = _make_api(tmp_path)
    api.attach_window(FakeWindow())

    result = api.export_logs("log")

    assert result["ok"] is False


# ---- closing the window ---------------------------------------------------


class FakeShutdownWindow:
    def __init__(self):
        self.evaluated = []
        self.destroyed = 0

    def evaluate_js(self, script):
        self.evaluated.append(script)

    def destroy(self):
        self.destroyed += 1


class StubApi:
    def __init__(self, wants_tray: bool = False):
        self._wants_tray = wants_tray

    def wants_tray_on_close(self) -> bool:
        return self._wants_tray


class StubTray:
    def __init__(self, installable: bool = True):
        self._installable = installable
        self.hidden = 0

    def install(self) -> bool:
        return self._installable

    def hide_window(self) -> None:
        self.hidden += 1

    def remove(self) -> None:
        pass


def test_the_first_close_click_cancels_the_close_and_starts_teardown():
    """The window must stay up while the background teardown runs, or the
    close-looked-like-a-freeze bug this replaced comes right back."""
    window, runtime, tray = FakeShutdownWindow(), SlowRuntime(), StubTray()
    api = StubApi(wants_tray=False)
    closing = threading.Event()

    result = desktop._on_closing(window, runtime, tray, api, closing)

    assert result is False
    assert closing.is_set()
    runtime.release.set()


def test_the_teardowns_own_destroy_call_is_not_cancelled_by_itself():
    """window.destroy() calls the native Close(), which fires FormClosing -
    the same 'closing' event - a second time. The old handler returned False
    unconditionally, so that second pass cancelled its own Close() forever:
    the overlay ran to completion but the window, and the process behind it,
    never actually went away. Once closing is already set, this must return
    True and let the real close through."""
    window, runtime, tray = FakeShutdownWindow(), SlowRuntime(), StubTray()
    api = StubApi(wants_tray=False)
    closing = threading.Event()
    closing.set()

    result = desktop._on_closing(window, runtime, tray, api, closing)

    assert result is True


class SlowRuntime:
    """A runtime whose teardown takes long enough to notice, which is the whole
    problem the overlay exists for."""

    def __init__(self):
        self.shutdown_calls = 0
        self.release = threading.Event()

    def shutdown(self):
        self.shutdown_calls += 1
        self.release.wait(timeout=5)


class FakeTray:
    def __init__(self):
        self.removed = 0

    def remove(self):
        self.removed += 1


def test_the_window_is_not_destroyed_until_the_teardown_finishes():
    """Closing takes seconds - taskkill, sockets, the WinDivert filter. The
    old handler did it on the GUI thread and returned True, so the window went
    away while the work was still running and the app looked frozen first."""
    window, runtime, tray = FakeShutdownWindow(), SlowRuntime(), FakeTray()
    closing = threading.Event()

    desktop._begin_shutdown(window, runtime, tray, closing)

    # Still tearing down: the overlay eventually goes up and nothing has
    # closed yet. evaluate_js() itself now runs on the teardown thread (see
    # _begin_shutdown's docstring on why it must not run on the GUI thread),
    # so it is not necessarily there the instant this call returns.
    deadline = time.monotonic() + 5
    while not window.evaluated and time.monotonic() < deadline:
        time.sleep(0.01)
    assert window.evaluated == [desktop._CLOSING_EVENT_JS]
    assert window.destroyed == 0

    runtime.release.set()
    deadline = time.monotonic() + 5
    while window.destroyed == 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert window.destroyed == 1
    assert tray.removed == 1


def test_a_second_close_click_does_not_start_a_second_teardown():
    """An app that looks frozen is an app people click again."""
    window, runtime, tray = FakeShutdownWindow(), SlowRuntime(), FakeTray()
    closing = threading.Event()

    desktop._begin_shutdown(window, runtime, tray, closing)
    desktop._begin_shutdown(window, runtime, tray, closing)
    runtime.release.set()
    deadline = time.monotonic() + 5
    while window.destroyed == 0 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert runtime.shutdown_calls == 1
    assert window.destroyed == 1


def test_a_ui_that_cannot_be_told_still_shuts_down():
    """The overlay is a courtesy. A frontend that already crashed must not be
    able to keep the app running."""

    class Broken(FakeShutdownWindow):
        def evaluate_js(self, script):
            raise RuntimeError("the web view is gone")

    window, runtime, tray = Broken(), SlowRuntime(), FakeTray()
    runtime.release.set()

    desktop._begin_shutdown(window, runtime, tray, threading.Event())

    deadline = time.monotonic() + 5
    while window.destroyed == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert window.destroyed == 1


def test_a_teardown_that_fails_still_closes_the_window():
    """Otherwise a half-broken runtime leaves a window nobody can get rid of."""

    class Exploding(SlowRuntime):
        def shutdown(self):
            raise RuntimeError("zapret refused to die")

    window, tray = FakeShutdownWindow(), FakeTray()

    desktop._begin_shutdown(window, Exploding(), tray, threading.Event())

    deadline = time.monotonic() + 5
    while window.destroyed == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert window.destroyed == 1


# ---- when the tray icon exists --------------------------------------------


class SpyTray:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.installs = 0
        self.removes = 0
        self.hides = 0
        self.can_install = True
        self.available = False

    def install(self):
        self.installs += 1
        self.available = self.can_install
        return self.can_install

    def remove(self):
        self.removes += 1
        self.available = False

    def hide_window(self):
        self.hides += 1


def _main_with_spy_tray(monkeypatch, *, minimize_to_tray: bool):
    """Run main() far enough to collect the handlers it registered, with the
    tray and the window faked out."""
    monkeypatch.setattr(sys, "argv", ["prog", "--dev"])
    window = _FakeWindow()
    window.events.loaded = _FakeEvent()
    monkeypatch.setattr(desktop.webview, "create_window", lambda *a, **k: window)
    monkeypatch.setattr(desktop.webview, "start", lambda: None)

    config = Config()
    config.ui.minimize_to_tray = minimize_to_tray
    monkeypatch.setattr(desktop, "load_config", lambda path: config)

    trays: list[SpyTray] = []

    def make_tray(*args, **kwargs):
        tray = SpyTray(*args, **kwargs)
        trays.append(tray)
        return tray

    monkeypatch.setattr(desktop, "TrayIcon", make_tray)
    desktop.main(admin_check=lambda: True, startup_check_delay_s=0)
    return window, trays[0]


def test_the_tray_icon_does_not_appear_while_the_window_is_on_screen(monkeypatch):
    """It stands in for a window you cannot see. Next to a visible one it is
    just clutter in a tray people keep tidy."""
    window, tray = _main_with_spy_tray(monkeypatch, minimize_to_tray=True)

    assert tray.installs == 0, "the tray was installed before anything was hidden"

    for handler in window.events.shown.handlers:
        handler()

    assert tray.installs == 0
    assert tray.removes >= 1, "showing the window must take the icon back down"

    for handler in window.events.closing.handlers:
        handler()


def test_hiding_to_the_tray_installs_the_icon_first(monkeypatch):
    window, tray = _main_with_spy_tray(monkeypatch, minimize_to_tray=True)

    kept_open = window.events.closing.handlers[0]()

    assert kept_open is False, "pywebview must be told to cancel the close"
    assert tray.installs == 1
    assert tray.hides == 1


def test_a_tray_that_cannot_be_created_does_not_swallow_the_close(monkeypatch):
    """Otherwise the setting leaves a window nobody can get rid of: the close
    is cancelled and there is no icon to bring it back from."""
    window, tray = _main_with_spy_tray(monkeypatch, minimize_to_tray=True)
    tray.can_install = False

    window.events.closing.handlers[0]()

    assert tray.installs == 1
    assert tray.hides == 0  # fell through to a real shutdown instead


def test_the_tray_is_never_touched_when_the_setting_is_off(monkeypatch):
    """"Не инициализировать ни при каких условиях" - the icon must not so much
    as be attempted for somebody who turned the feature off."""
    window, tray = _main_with_spy_tray(monkeypatch, minimize_to_tray=False)

    window.events.closing.handlers[0]()

    assert tray.installs == 0
    assert tray.hides == 0


def test_the_tray_menu_can_tell_whether_the_bridge_is_running(monkeypatch):
    """"Остановить мост" is greyed out from this callback; without it the item
    is always clickable and sometimes does nothing."""
    _, tray = _main_with_spy_tray(monkeypatch, minimize_to_tray=True)

    assert callable(tray.kwargs["bridge_running"])
    assert tray.kwargs["bridge_running"]() is False


def _main_capturing_create_window(monkeypatch, *, argv, autostart_minimized: bool, minimize_to_tray: bool):
    """Like _main_with_spy_tray, but returns what main() actually passed to
    webview.create_window - the `hidden` kwarg is the whole point here, and
    the other helper's fake discards kwargs entirely."""
    monkeypatch.setattr(sys, "argv", argv)
    window = _FakeWindow()
    window.events.loaded = _FakeEvent()
    created: dict = {}

    def fake_create_window(title, url, **kwargs):
        created.update(kwargs)
        return window

    monkeypatch.setattr(desktop.webview, "create_window", fake_create_window)
    monkeypatch.setattr(desktop.webview, "start", lambda: None)

    config = Config()
    config.ui.autostart_minimized = autostart_minimized
    config.ui.minimize_to_tray = minimize_to_tray
    monkeypatch.setattr(desktop, "load_config", lambda path: config)
    monkeypatch.setattr(desktop, "TrayIcon", SpyTray)

    desktop.main(admin_check=lambda: True, startup_check_delay_s=0)
    return created


def test_minimized_autostart_launch_creates_a_hidden_window_even_with_minimize_to_tray_off(monkeypatch):
    """BUG FIX regression: the window used to open visible on every minimized
    autostart launch unless "Сворачивать в трей при закрытии" (an unrelated
    setting) happened to also be on, because `hidden` read
    config.ui.minimize_to_tray instead of config.ui.autostart_minimized. This
    pins the fix with the tray-on-close setting deliberately OFF, so the old
    bug (wrong field) cannot pass by coincidence."""
    created = _main_capturing_create_window(
        monkeypatch, argv=["prog", "--dev", "--minimized"],
        autostart_minimized=True, minimize_to_tray=False,
    )

    assert created["hidden"] is True


def test_a_normal_launch_is_never_hidden_even_with_autostart_minimized_on(monkeypatch):
    """The flag has to come from HOW the process was launched, not merely
    from a config value that says what a future autostart launch should do -
    otherwise every ordinary double-click while the setting is on would open
    invisibly."""
    created = _main_capturing_create_window(
        monkeypatch, argv=["prog", "--dev"],
        autostart_minimized=True, minimize_to_tray=True,
    )

    assert created["hidden"] is False


# ---- smooth window reveal: native background matches the boot skeleton ---


def test_boot_background_color_picks_the_right_theme():
    assert desktop._boot_background_color("dark") == desktop.BOOT_BACKGROUND_DARK
    assert desktop._boot_background_color("light") == desktop.BOOT_BACKGROUND_LIGHT


def test_boot_background_colors_match_the_frontend_skeleton():
    """Keeps desktop.py's BOOT_BACKGROUND_* honest against
    frontend/index.html's --bb-boot-bg, the same way frontend/test/
    bootSkeleton.test.ts already keeps index.html honest against
    tokens.css - a mismatch here is a real, visible flash between the
    native window's first paint and the HTML skeleton's own, on whichever
    theme drifted."""
    import re

    index_html = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
    if not index_html.exists():
        return  # not every checkout has the frontend tree present

    text = index_html.read_text(encoding="utf-8")
    dark_region = text[text.index("[data-theme='dark']") :]
    light_match = re.search(r"--bb-boot-bg:\s*([^;]+);", text[: text.index("[data-theme='dark']")])
    dark_match = re.search(r"--bb-boot-bg:\s*([^;]+);", dark_region)

    assert light_match and light_match.group(1).strip().lower() == desktop.BOOT_BACKGROUND_LIGHT
    assert dark_match and dark_match.group(1).strip().lower() == desktop.BOOT_BACKGROUND_DARK


def test_main_passes_the_boot_background_color_to_create_window(monkeypatch):
    created = _main_capturing_create_window(
        monkeypatch, argv=["prog", "--dev"], autostart_minimized=False, minimize_to_tray=False,
    )

    assert created["background_color"] == desktop.BOOT_BACKGROUND_DARK  # Config()'s theme default


def test_open_external_url_calls_webbrowser_open(tmp_path: Path, monkeypatch):
    opened = []
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: opened.append(url))
    api = _make_api(tmp_path)

    result = api.open_external_url("https://github.com/getonjbghelp/bridgebox/issues")

    assert result == {"ok": True, "error": None}
    assert opened == ["https://github.com/getonjbghelp/bridgebox/issues"]


def test_open_external_url_rejects_a_non_http_scheme(tmp_path: Path, monkeypatch):
    opened = []
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: opened.append(url))
    api = _make_api(tmp_path)

    result = api.open_external_url("javascript:alert(1)")

    assert result["ok"] is False
    assert opened == []
