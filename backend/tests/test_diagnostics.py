import subprocess
from pathlib import Path

from bridgebox.diagnostics import build_probe, build_switch
from bridgebox.zapret.process import NEW_CONSOLE, NO_WINDOW, ZapretProcess
from bridgebox.zapret.strategies import discover_strategies


class FakeResponse:
    async def read(self):
        return b""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self):
        self.get_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append(url)
        return FakeResponse()


class FakeLauncher:
    def __init__(self, pid: int = 1234):
        self.pid = pid
        self.calls = []
        # Separate from .calls (which every existing test asserts the exact
        # shape of) rather than folding creationflags/capture_output into it -
        # additive, so it cannot change what those tests already check.
        self.kwargs_calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        self.kwargs_calls.append(kwargs)

        class Result:
            pid = self.pid

            def poll(self):
                return None  # still running - ZapretProcess.stop() reads this

        return Result()


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)

        class Result:
            returncode = 0

        return Result()


async def test_build_probe_measures_elapsed_time_against_real_upstream():
    session = FakeSession()
    probe = build_probe(session)

    elapsed = await probe()

    assert isinstance(elapsed, float)
    assert elapsed >= 0
    assert session.get_calls == ["https://ecast.jackboxgames.com/api/v2/rooms/ZZZZ"]


async def test_build_switch_stops_running_zapret_then_starts_resolved_strategy(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "General.bat").write_text("@echo off\n")
    (strategies_dir / "Alternative 1.bat").write_text("@echo off\n")
    strategies = discover_strategies(strategies_dir)

    launcher = FakeLauncher()
    runner = FakeRunner()
    zapret_process = ZapretProcess(popen=launcher, runner=runner, allowed_root=tmp_path)
    zapret_process.start(strategies["general"].path)

    switch = build_switch(zapret_process, strategies)
    await switch("alternative-1")

    # stopped the running "general" instance, then started "alternative-1".
    # One tree kill scoped to our own pid - switching strategies mid-suite must
    # not reach a winws the user started outside BridgeBox.
    assert runner.calls == [["taskkill", "/F", "/T", "/PID", "1234"]]
    assert launcher.calls[-1] == [str(strategies["alternative-1"].path)]
    assert zapret_process.is_running is True


async def test_build_switch_when_nothing_running_just_starts(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "General.bat").write_text("@echo off\n")
    strategies = discover_strategies(strategies_dir)

    launcher = FakeLauncher()
    runner = FakeRunner()
    zapret_process = ZapretProcess(popen=launcher, runner=runner, allowed_root=tmp_path)

    switch = build_switch(zapret_process, strategies)
    await switch("general")

    assert runner.calls == []  # nothing to stop
    assert zapret_process.is_running is True


async def test_build_switch_hides_the_console_by_default(tmp_path: Path):
    """The bug this guards: every strategy switch used to call
    zapret_process.start() with no creationflags at all, which meant a
    console flashed open once per strategy for as long as the suite ran -
    regardless of the "Скрывать консоль" setting. build_switch's own
    hide_console default (True) is what makes "forgot to pass it" fail safe
    instead of flashing consoles during the wizard's autotest."""
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "General.bat").write_text("@echo off\n")
    strategies = discover_strategies(strategies_dir)

    launcher = FakeLauncher()
    zapret_process = ZapretProcess(popen=launcher, runner=FakeRunner(), allowed_root=tmp_path)

    switch = build_switch(zapret_process, strategies)
    await switch("general")

    kwargs = launcher.kwargs_calls[-1]
    assert kwargs["creationflags"] == NO_WINDOW
    # capture_output itself is not forwarded to Popen - ZapretProcess.start()
    # turns it into stdout=PIPE (see process.py). A hidden console's output
    # has nowhere else to go, so it must be piped into the app log instead of
    # silently discarded - same pairing RuntimeCore._start() uses for the
    # main bridge.
    assert kwargs.get("stdout") == subprocess.PIPE


async def test_build_switch_can_show_the_console_when_asked(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "General.bat").write_text("@echo off\n")
    strategies = discover_strategies(strategies_dir)

    launcher = FakeLauncher()
    zapret_process = ZapretProcess(popen=launcher, runner=FakeRunner(), allowed_root=tmp_path)

    switch = build_switch(zapret_process, strategies, hide_console=False)
    await switch("general")

    kwargs = launcher.kwargs_calls[-1]
    assert kwargs["creationflags"] == NEW_CONSOLE
    # A visible console is the output - piping it away would empty the very
    # window the user asked to see (see process.py's start()).
    assert "stdout" not in kwargs
