from pathlib import Path

import pytest

from bridgebox.config import Config
from bridgebox.runtime_core import RuntimeCore
from bridgebox.tls.ca import CA_CERT_FILENAME, CertPaths
from bridgebox.zapret.process import ZapretProcess


class FakeSession:
    def __init__(self):
        self.closed_calls = 0

    async def close(self):
        self.closed_calls += 1


class FakeRunner:
    def __init__(self):
        self.cleanup_calls = 0

    async def cleanup(self):
        self.cleanup_calls += 1


class FakeLauncher:
    def __init__(self, pid: int = 4242):
        self.pid = pid
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, "kwargs": kwargs})

        class Result:
            pid = self.pid

            def poll(self):
                return None  # still running - ZapretProcess.stop() reads this

        return Result()


class FakeSubprocessRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)

        class Result:
            returncode = 0

        return Result()


def _make_deps(tmp_path: Path, *, install_ca_result: bool = True):
    calls = {
        "generate_leaf_cert": [],
        "install_ca_windows": [],
        "session_factory": [],
        "build_full_app": [],
        "run_server": [],
        "build_ssl_context": [],
    }
    cert_paths = CertPaths(cert=tmp_path / "leaf.pem", key=tmp_path / "leaf-key.pem")

    def fake_generate_leaf_cert(cert_dir):
        calls["generate_leaf_cert"].append(cert_dir)
        return cert_paths

    def fake_install_ca_windows(ca_cert_path):
        calls["install_ca_windows"].append(ca_cert_path)
        return install_ca_result

    def fake_session_factory():
        calls["session_factory"].append(True)
        return FakeSession()

    def fake_build_full_app(**kwargs):
        calls["build_full_app"].append(kwargs)
        return object()

    async def fake_run_server(app, host, port, *, ssl_context=None):
        calls["run_server"].append({"app": app, "host": host, "port": port, "ssl": ssl_context})
        return FakeRunner()

    def fake_build_ssl_context(cert, key):
        calls["build_ssl_context"].append((cert, key))
        return object()

    return calls, {
        "generate_leaf_cert": fake_generate_leaf_cert,
        "install_ca_windows": fake_install_ca_windows,
        "session_factory": fake_session_factory,
        "build_full_app": fake_build_full_app,
        "run_server": fake_run_server,
        "build_ssl_context": fake_build_ssl_context,
        "upstream_client_factory": lambda session: ("upstream", session),
        "ws_connector_factory": lambda session: ("ws", session),
    }


async def test_set_config_replaces_the_config_used_by_a_later_start(tmp_path: Path):
    strategies_dir = tmp_path / "zapret" / "strategies"
    strategies_dir.mkdir(parents=True)
    (strategies_dir / "General.bat").write_text("@echo off\n")
    (strategies_dir / "Alternative 1.bat").write_text("@echo off\n")

    config = Config()
    config.zapret.enabled = True
    config.zapret.dir = "zapret"
    config.zapret.strategy = "general"

    calls, deps = _make_deps(tmp_path)
    launcher = FakeLauncher(pid=5555)
    zapret_process = ZapretProcess(popen=launcher, runner=FakeSubprocessRunner(), allowed_root=tmp_path)
    core = RuntimeCore(
        config=config, project_root=tmp_path, zapret_process=zapret_process, **deps
    )

    updated = Config()
    updated.zapret.enabled = True
    updated.zapret.dir = "zapret"
    updated.zapret.strategy = "alternative-1"
    core.set_config(updated)

    status = await core.start()

    assert status["zapretRunning"] is True
    assert launcher.calls[0]["cmd"] == [str(strategies_dir / "Alternative 1.bat")]


def test_zapret_process_property_exposes_injected_instance(tmp_path: Path):
    config = Config()
    config.zapret.enabled = False
    zapret_process = ZapretProcess(popen=FakeLauncher(), runner=FakeSubprocessRunner(), allowed_root=tmp_path)
    core = RuntimeCore(config=config, project_root=tmp_path, zapret_process=zapret_process)

    assert core.zapret_process is zapret_process


def test_status_when_nothing_started(tmp_path: Path):
    config = Config()
    config.zapret.enabled = False
    core = RuntimeCore(config=config, project_root=tmp_path)

    status = core.status()

    assert status["running"] is False
    assert status["zapretRunning"] is False


async def test_start_happy_path_zapret_disabled(tmp_path: Path):
    config = Config()
    config.zapret.enabled = False
    calls, deps = _make_deps(tmp_path)
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)

    status = await core.start()

    assert status["running"] is True
    assert status["zapretRunning"] is False
    assert status["zapretError"] is None
    assert len(calls["generate_leaf_cert"]) == 1
    assert len(calls["session_factory"]) == 1
    assert len(calls["build_full_app"]) == 1
    assert len(calls["run_server"]) == 1
    # install_ca_windows runs on first start (no marker file yet)
    assert len(calls["install_ca_windows"]) == 1


async def test_start_is_idempotent(tmp_path: Path):
    config = Config()
    config.zapret.enabled = False
    calls, deps = _make_deps(tmp_path)
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)

    await core.start()
    await core.start()

    assert len(calls["run_server"]) == 1  # second call was a no-op, returned cached status


async def test_start_installs_ca_once_via_marker(tmp_path: Path):
    config = Config()
    config.zapret.enabled = False
    calls, deps = _make_deps(tmp_path)
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)
    await core.start()
    await core.stop()

    # second independent start (new core instance, same cert_dir) should see
    # the marker file from the first run and skip re-installing the CA
    calls2, deps2 = _make_deps(tmp_path)
    core2 = RuntimeCore(config=config, project_root=tmp_path, **deps2)
    await core2.start()

    assert len(calls2["install_ca_windows"]) == 0
    status = core2.status()
    assert status["certInstalled"] is True


async def test_start_with_zapret_enabled_resolves_and_starts_strategy(tmp_path: Path):
    strategies_dir = tmp_path / "zapret" / "strategies"
    strategies_dir.mkdir(parents=True)
    (strategies_dir / "General.bat").write_text("@echo off\n")

    config = Config()
    config.zapret.enabled = True
    config.zapret.dir = "zapret"
    config.zapret.strategy = "general"

    calls, deps = _make_deps(tmp_path)
    launcher = FakeLauncher(pid=9999)
    zapret_process = ZapretProcess(popen=launcher, runner=FakeSubprocessRunner(), allowed_root=tmp_path)
    core = RuntimeCore(
        config=config, project_root=tmp_path, zapret_process=zapret_process, **deps
    )

    status = await core.start()

    assert status["zapretRunning"] is True
    assert status["zapretPid"] == 9999
    assert status["zapretError"] is None
    assert launcher.calls[0]["cmd"] == [str(strategies_dir / "General.bat")]


async def test_start_zapret_failure_does_not_roll_back_server(tmp_path: Path):
    # no strategies dir on disk -> discover_strategies finds nothing ->
    # resolve_strategy raises KeyError
    config = Config()
    config.zapret.enabled = True
    config.zapret.dir = "zapret"
    config.zapret.strategy = "general"

    calls, deps = _make_deps(tmp_path)
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)

    status = await core.start()

    assert status["running"] is True  # server still came up
    assert status["zapretRunning"] is False
    assert status["zapretError"] is not None
    assert len(calls["run_server"]) == 1  # run_server was never torn down


async def test_stop_happy_path_tears_down_everything(tmp_path: Path):
    strategies_dir = tmp_path / "zapret" / "strategies"
    strategies_dir.mkdir(parents=True)
    (strategies_dir / "General.bat").write_text("@echo off\n")

    config = Config()
    config.zapret.enabled = True
    config.zapret.dir = "zapret"
    config.zapret.strategy = "general"

    calls, deps = _make_deps(tmp_path)
    zapret_process = ZapretProcess(popen=FakeLauncher(), runner=FakeSubprocessRunner(), allowed_root=tmp_path)
    core = RuntimeCore(
        config=config, project_root=tmp_path, zapret_process=zapret_process, **deps
    )
    await core.start()

    status = await core.stop()

    assert status["running"] is False
    assert status["zapretRunning"] is False
    assert zapret_process.is_running is False


async def test_stop_is_idempotent_when_never_started(tmp_path: Path):
    config = Config()
    config.zapret.enabled = False
    core = RuntimeCore(config=config, project_root=tmp_path)

    status = await core.stop()

    assert status["running"] is False


async def test_a_failed_bind_closes_the_session_instead_of_leaking_it(tmp_path: Path):
    """Binding fails routinely - the port is still held by a previous instance
    or another app owns it. The session used to be assigned to self before the
    bind, so start() raised, stop() never ran, and the next attempt sailed
    past the `self._runner is not None` guard and overwrote the reference.
    Five clicks on a failing button leaked five connectors."""
    sessions: list[FakeSession] = []

    def session_factory():
        session = FakeSession()
        sessions.append(session)
        return session

    async def refuse_to_bind(*args, **kwargs):
        raise OSError("[WinError 10048] port already in use")

    core = RuntimeCore(
        config=Config(),
        project_root=tmp_path,
        zapret_process=ZapretProcess(
            popen=FakeLauncher(), runner=FakeSubprocessRunner(), allowed_root=tmp_path
        ),
        session_factory=session_factory,
        build_full_app=lambda **kwargs: object(),
        run_server=refuse_to_bind,
        build_ssl_context=lambda cert, key: None,
        generate_leaf_cert=lambda cert_dir: CertPaths(tmp_path / "c.pem", tmp_path / "k.pem"),
        install_ca_windows=lambda cert: True,
        upstream_client_factory=lambda session: object(),
        ws_connector_factory=lambda session: object(),
    )

    for _ in range(3):
        with pytest.raises(OSError):
            await core.start()

    assert len(sessions) == 3
    assert [s.closed_calls for s in sessions] == [1, 1, 1]
    assert core.status()["running"] is False


@pytest.mark.asyncio
async def test_the_ca_is_what_gets_installed_not_the_leaf(tmp_path: Path):
    """install_ca_windows used to be handed cert_paths.cert - the localhost
    LEAF - so the server certificate itself was trusted directly and the CA
    never entered the store at all. It worked, because a self-trusted leaf
    needs no chain, but it meant reissuing the leaf silently lost its trust
    and removing "BridgeBox Local CA" by subject could never match what was
    actually installed (the installed cert's subject is CN=localhost)."""
    calls, deps = _make_deps(tmp_path)
    core = RuntimeCore(config=Config(), project_root=tmp_path, **deps)

    await core.start()

    assert len(calls["install_ca_windows"]) == 1
    installed = Path(calls["install_ca_windows"][0])
    assert installed.name == CA_CERT_FILENAME
    assert installed.name != "leaf.pem"


async def test_ensure_certificate_works_without_starting_anything(tmp_path: Path):
    """The wizard's «Установить сертификат» path. It has to issue and trust
    exactly what start() would, without binding a port or launching winws -
    otherwise a first-run user pressing an install button gets a running
    bridge they never asked for."""
    config = Config()
    calls, deps = _make_deps(tmp_path)
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)

    cert_paths, installed = core.ensure_certificate()

    assert installed is True
    assert cert_paths.cert == tmp_path / "leaf.pem"
    assert len(calls["install_ca_windows"]) == 1
    # The CA, never the leaf - this is the exact swap that shipped broken once.
    assert calls["install_ca_windows"][0].name == CA_CERT_FILENAME
    assert calls["run_server"] == [], "no listener may be bound by a certificate install"
    assert calls["session_factory"] == []


async def test_ensure_certificate_is_idempotent_via_the_marker(tmp_path: Path):
    config = Config()
    _, deps = _make_deps(tmp_path)
    RuntimeCore(config=config, project_root=tmp_path, **deps).ensure_certificate()

    calls2, deps2 = _make_deps(tmp_path)
    _, installed = RuntimeCore(config=config, project_root=tmp_path, **deps2).ensure_certificate()

    assert installed is True
    assert calls2["install_ca_windows"] == [], "the marker must short-circuit a second certutil run"


async def test_ensure_certificate_reports_a_refused_install(tmp_path: Path):
    config = Config()
    _, deps = _make_deps(tmp_path, install_ca_result=False)
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)

    _, installed = core.ensure_certificate()

    assert installed is False
    # No marker, so the next attempt actually retries instead of assuming.
    cert_dir = tmp_path / "certs"
    assert not (cert_dir / ".ca-installed").exists()


async def test_start_still_installs_the_ca_through_the_shared_path(tmp_path: Path):
    """Guards the extraction itself: start() must keep going through
    ensure_certificate() rather than growing its own second copy of it."""
    config = Config()
    config.zapret.enabled = False
    calls, deps = _make_deps(tmp_path)
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)
    seen = []
    original = core.ensure_certificate
    core.ensure_certificate = lambda: (seen.append(True), original())[1]

    await core.start()
    await core.stop()

    assert seen == [True]
    assert len(calls["install_ca_windows"]) == 1


# ---- zapret dying on its own ----


async def test_a_zapret_that_dies_on_its_own_leaves_a_notice(tmp_path: Path):
    """The user closed the winws console by hand. Reporting a stopped bridge
    with no explanation is what makes that look like the app breaking."""
    from bridgebox.runtime_core import CONSOLE_CLOSED_NOTICE

    config = Config()
    config.zapret.enabled = False
    calls, deps = _make_deps(tmp_path)
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)
    told = []
    core.set_zapret_exit_handler(lambda: told.append(True))

    await core.start()
    core._on_zapret_exit(1)
    await core.stop()

    assert told == [True], "nobody was told to tear the bridge down"
    assert core.status()["zapretNotice"] == CONSOLE_CLOSED_NOTICE
    assert core.status()["running"] is False


async def test_starting_again_clears_the_previous_notice(tmp_path: Path):
    """A notice that outlives the run it describes is worse than none - it
    reads as a bridge that just failed."""
    config = Config()
    config.zapret.enabled = False
    calls, deps = _make_deps(tmp_path)
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)

    await core.start()
    core._on_zapret_exit(1)
    await core.stop()
    assert core.status()["zapretNotice"]

    await core.start()

    assert core.status()["zapretNotice"] is None


async def test_the_console_setting_decides_both_the_flags_and_the_capture(tmp_path: Path):
    """Hidden means "put the output in the log"; visible means "leave it in the
    window the user asked for". One setting, and it has to drive both."""
    from bridgebox.zapret.process import NEW_CONSOLE, NO_WINDOW

    strategies_dir = tmp_path / "zapret" / "strategies"
    strategies_dir.mkdir(parents=True)
    (strategies_dir / "General.bat").write_text("@echo off\n")

    for hide, expected_flags in ((True, NO_WINDOW), (False, NEW_CONSOLE)):
        config = Config()
        config.zapret.enabled = True
        config.zapret.dir = "zapret"
        config.zapret.strategy = "general"
        config.zapret.hide_console = hide

        calls, deps = _make_deps(tmp_path)
        launcher = FakeLauncher(pid=1234)
        zapret_process = ZapretProcess(
            popen=launcher, runner=FakeSubprocessRunner(), allowed_root=tmp_path
        )
        core = RuntimeCore(
            config=config, project_root=tmp_path, zapret_process=zapret_process, **deps
        )

        await core.start()

        kwargs = launcher.calls[0]["kwargs"]
        assert kwargs["creationflags"] == expected_flags
        assert ("stdout" in kwargs) is hide


async def test_two_concurrent_stops_tear_down_each_listener_once(tmp_path: Path):
    """The regression. stop() checks a field, awaits, then acts on what it
    checked, and several callers can reach it at once - the UI toggle, the
    updater, the strategy suite, the zapret watchdog and the app's own
    shutdown. Unguarded, two of them both saw a live socket.io runner and both
    cleaned it up, which the real log showed as "blobcast socket.io listener
    stopped" three times in one teardown."""
    import asyncio

    class SlowRunner:
        """cleanup() that actually suspends, the way web.AppRunner's does.

        The suspension is the whole test. FakeRunner's cleanup has no await in
        it, so it never yields, the next stop() is never scheduled in the
        middle of it, and the race cannot happen - which is how the first
        version of this test passed against the bug it was written to catch."""

        def __init__(self):
            self.cleanup_calls = 0

        async def cleanup(self):
            self.cleanup_calls += 1
            await asyncio.sleep(0)

    config = Config()
    config.zapret.enabled = False
    calls, deps = _make_deps(tmp_path)

    socketio_runner = SlowRunner()
    main_runner = SlowRunner()
    runners = iter([main_runner, socketio_runner])

    async def fake_run_server(app, host, port, *, ssl_context=None):
        calls["run_server"].append({"port": port})
        return next(runners)

    deps["run_server"] = fake_run_server

    class AppWithSocketio(dict):
        pass

    socketio_app = object()
    app = AppWithSocketio()

    def fake_build_full_app(**kwargs):
        from bridgebox.server.factory import BLOBCAST_SOCKETIO_APP

        app[BLOBCAST_SOCKETIO_APP] = socketio_app
        return app

    deps["build_full_app"] = fake_build_full_app

    core = RuntimeCore(config=config, project_root=tmp_path, **deps)
    await core.start()
    assert socketio_runner.cleanup_calls == 0

    await asyncio.gather(core.stop(), core.stop(), core.stop())

    assert socketio_runner.cleanup_calls == 1, "the socket.io listener was torn down twice"
    assert main_runner.cleanup_calls == 1, "the bridge server was torn down twice"


# ---- health check's first round (formerly a separate upstream pre-warm) ----


async def test_health_check_first_round_fires_immediately_without_zapret(
    tmp_path: Path, monkeypatch
):
    """No zapret means no settling to wait for - the first probe must not
    sit through a full HEALTH_CHECK_INTERVAL_S before firing. This is the
    gap the old one-shot prewarm existed to cover; the interval below is set
    absurdly long so the assertion can only pass if the first round really
    is immediate, not just eventually reached."""
    import asyncio

    from bridgebox import runtime_core

    monkeypatch.setattr(runtime_core, "HEALTH_CHECK_INTERVAL_S", 60)
    config = Config()
    config.zapret.enabled = False
    calls, deps = _make_deps(tmp_path)
    session = _FakeHealthSession(healthy=True)
    deps["session_factory"] = lambda: session
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)

    await core.start()
    await asyncio.sleep(0.05)

    assert session.get_calls > 0
    assert core.health_status() == {"ok": True, "consecutiveFailures": 0}


async def test_health_check_first_round_waits_for_winws_to_settle_when_zapret_is_enabled(
    tmp_path: Path, monkeypatch
):
    import asyncio

    from bridgebox import runtime_core

    monkeypatch.setattr(runtime_core, "STRATEGY_SETTLE_S", 0.05)
    # Also absurdly long, so a round only happens here because the settle
    # wait finished, not because the interval did.
    monkeypatch.setattr(runtime_core, "HEALTH_CHECK_INTERVAL_S", 60)

    strategies_dir = tmp_path / "zapret" / "strategies"
    strategies_dir.mkdir(parents=True)
    (strategies_dir / "General.bat").write_text("@echo off\n")

    config = Config()
    config.zapret.enabled = True
    config.zapret.dir = "zapret"
    config.zapret.strategy = "general"

    calls, deps = _make_deps(tmp_path)
    session = _FakeHealthSession(healthy=True)
    deps["session_factory"] = lambda: session
    zapret_process = ZapretProcess(popen=FakeLauncher(), runner=FakeSubprocessRunner(), allowed_root=tmp_path)
    core = RuntimeCore(config=config, project_root=tmp_path, zapret_process=zapret_process, **deps)

    await core.start()
    assert session.get_calls == 0, "must not probe before WinDivert has settled into the packet path"

    await asyncio.sleep(0.1)

    assert session.get_calls > 0


async def test_a_stop_racing_a_start_does_not_interleave(tmp_path: Path):
    """Same gate, the other direction: the toggle and the automatic
    start-on-launch can both land at once."""
    import asyncio

    config = Config()
    config.zapret.enabled = False
    calls, deps = _make_deps(tmp_path)
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)

    await asyncio.gather(core.start(), core.start())

    # The second start saw the first one finished and did nothing, rather than
    # binding a second pair of listeners.
    assert len(calls["run_server"]) == 1


class _FakeProbeResponse:
    def __init__(self, status: int = 200):
        self.status = status
        self.headers: dict = {}

    async def read(self):
        return b"{}"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeHealthSession:
    """Feeds _health_check_loop's probe_targets calls - configurable
    success/failure per round (`.healthy` can be flipped mid-test), unlike
    the module's own FakeSession above, which only ever supports .close()."""

    def __init__(self, *, healthy: bool):
        self.healthy = healthy
        self.get_calls = 0

    def get(self, url, **kwargs):
        self.get_calls += 1
        if not self.healthy:
            raise ConnectionError("boom")
        return _FakeProbeResponse()

    async def close(self):
        pass


async def test_health_check_task_is_not_started_when_disabled(tmp_path: Path):
    config = Config()
    config.zapret.enabled = False
    config.health_check.enabled = False
    calls, deps = _make_deps(tmp_path)
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)

    await core.start()

    assert core._health_task is None
    assert core.health_status() is None


async def test_health_check_task_starts_when_enabled(tmp_path: Path):
    config = Config()
    config.zapret.enabled = False
    calls, deps = _make_deps(tmp_path)
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)

    await core.start()

    assert core._health_task is not None


async def test_health_check_flags_unhealthy_after_consecutive_failures(
    tmp_path: Path, monkeypatch
):
    import asyncio

    from bridgebox import runtime_core

    monkeypatch.setattr(runtime_core, "HEALTH_CHECK_INTERVAL_S", 0.01)
    config = Config()
    config.zapret.enabled = False
    calls, deps = _make_deps(tmp_path)
    deps["session_factory"] = lambda: _FakeHealthSession(healthy=False)
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)

    await core.start()
    await asyncio.sleep(0.1)  # several rounds at the shrunk interval

    health = core.health_status()
    assert health is not None
    assert health["ok"] is False
    assert health["consecutiveFailures"] >= runtime_core.HEALTH_CHECK_FAILURE_THRESHOLD


async def test_health_check_recovers_once_a_round_succeeds(tmp_path: Path, monkeypatch):
    import asyncio

    from bridgebox import runtime_core

    monkeypatch.setattr(runtime_core, "HEALTH_CHECK_INTERVAL_S", 0.01)
    config = Config()
    config.zapret.enabled = False
    calls, deps = _make_deps(tmp_path)
    session = _FakeHealthSession(healthy=False)
    deps["session_factory"] = lambda: session
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)

    await core.start()
    await asyncio.sleep(0.1)
    assert core.health_status()["ok"] is False

    session.healthy = True
    await asyncio.sleep(0.1)

    health = core.health_status()
    assert health["ok"] is True
    assert health["consecutiveFailures"] == 0


async def test_stop_cancels_the_health_check_task_and_clears_the_report(
    tmp_path: Path, monkeypatch
):
    import asyncio

    from bridgebox import runtime_core

    monkeypatch.setattr(runtime_core, "HEALTH_CHECK_INTERVAL_S", 0.01)
    config = Config()
    config.zapret.enabled = False
    calls, deps = _make_deps(tmp_path)
    deps["session_factory"] = lambda: _FakeHealthSession(healthy=False)
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)

    await core.start()
    await asyncio.sleep(0.1)
    health_task = core._health_task
    assert health_task is not None

    await core.stop()

    assert core._health_task is None
    assert core.health_status() is None


class _FakeUpstreamClientWithActivity:
    """A minimal stand-in whose last_request_at can be set directly, to
    simulate real game traffic having (or not having) just passed through -
    see RECENT_ACTIVITY_SKIP_S."""

    last_request_at: float | None = None


async def test_health_check_round_is_skipped_shortly_after_real_traffic(
    tmp_path: Path, monkeypatch
):
    import asyncio
    import time

    from bridgebox import runtime_core

    monkeypatch.setattr(runtime_core, "HEALTH_CHECK_INTERVAL_S", 0.01)
    monkeypatch.setattr(runtime_core, "RECENT_ACTIVITY_SKIP_S", 10)
    config = Config()
    config.zapret.enabled = False
    calls, deps = _make_deps(tmp_path)
    session = _FakeHealthSession(healthy=True)
    deps["session_factory"] = lambda: session
    upstream_client = _FakeUpstreamClientWithActivity()
    deps["upstream_client_factory"] = lambda s: upstream_client
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)

    upstream_client.last_request_at = time.monotonic()  # "just happened"

    await core.start()
    await asyncio.sleep(0.05)

    assert session.get_calls == 0, "a probe must not fire while real traffic was just seen"
    assert core.health_status() is None


async def test_health_check_resumes_once_real_traffic_activity_goes_stale(
    tmp_path: Path, monkeypatch
):
    import asyncio
    import time

    from bridgebox import runtime_core

    monkeypatch.setattr(runtime_core, "HEALTH_CHECK_INTERVAL_S", 0.01)
    monkeypatch.setattr(runtime_core, "RECENT_ACTIVITY_SKIP_S", 10)
    config = Config()
    config.zapret.enabled = False
    calls, deps = _make_deps(tmp_path)
    session = _FakeHealthSession(healthy=True)
    deps["session_factory"] = lambda: session
    upstream_client = _FakeUpstreamClientWithActivity()
    deps["upstream_client_factory"] = lambda s: upstream_client
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)

    upstream_client.last_request_at = time.monotonic() - 3600  # long stale

    await core.start()
    await asyncio.sleep(0.05)

    assert session.get_calls > 0
    assert core.health_status() == {"ok": True, "consecutiveFailures": 0}
