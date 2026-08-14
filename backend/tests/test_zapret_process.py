import io
import logging
import subprocess
import threading
import time
from pathlib import Path

import pytest

from bridgebox.zapret.process import (
    NEW_CONSOLE,
    NO_WINDOW,
    ZapretProcess,
    console_flags,
    kill_all_winws,
    stop_windivert_service,
    wait_for_winws_exit,
    winws_is_running,
)


class FakePopenResult:
    def __init__(self, pid: int, exit_code: int | None = None):
        self.pid = pid
        self._exit_code = exit_code

    def poll(self):
        """None while running, an exit code once finished - same contract as
        subprocess.Popen. stop() reads this to decide whether there is
        anything left to kill."""
        return self._exit_code


class FakeLauncher:
    def __init__(self, pid: int = 4242, exit_code: int | None = None):
        self.pid = pid
        self.exit_code = exit_code
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, "kwargs": kwargs})
        return FakePopenResult(self.pid, self.exit_code)


class FakeRunner:
    def __init__(self):
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)

        class Result:
            returncode = 0

        return Result()


def test_start_launches_bat_in_its_own_dir_and_tracks_pid(tmp_path: Path):
    bat_path = tmp_path / "strategies" / "General.bat"
    bat_path.parent.mkdir(parents=True)
    bat_path.write_text("@echo off\n")

    launcher = FakeLauncher(pid=4242)
    zapret = ZapretProcess(popen=launcher, runner=FakeRunner(), allowed_root=tmp_path)

    pid = zapret.start(bat_path)

    assert pid == 4242
    assert zapret.is_running is True
    assert launcher.calls[0]["cmd"] == [str(bat_path)]
    assert launcher.calls[0]["kwargs"]["cwd"] == str(bat_path.parent)


class FakeJob:
    def __init__(self):
        self.assigned = []

    def assign(self, pid):
        self.assigned.append(pid)
        return True


def test_start_assigns_process_to_job_object(tmp_path: Path):
    bat_path = tmp_path / "General.bat"
    bat_path.write_text("@echo off\n")
    job = FakeJob()
    zapret = ZapretProcess(popen=FakeLauncher(pid=777), runner=FakeRunner(), job=job, allowed_root=tmp_path)

    zapret.start(bat_path)

    assert job.assigned == [777]


def test_start_forwards_creationflags_to_popen(tmp_path: Path):
    bat_path = tmp_path / "General.bat"
    bat_path.write_text("@echo off\n")
    launcher = FakeLauncher()
    zapret = ZapretProcess(popen=launcher, runner=FakeRunner(), allowed_root=tmp_path)

    zapret.start(bat_path, creationflags=0x08000000)

    assert launcher.calls[0]["kwargs"]["creationflags"] == 0x08000000


def test_start_default_creationflags_is_zero(tmp_path: Path):
    bat_path = tmp_path / "General.bat"
    bat_path.write_text("@echo off\n")
    launcher = FakeLauncher()
    zapret = ZapretProcess(popen=launcher, runner=FakeRunner(), allowed_root=tmp_path)

    zapret.start(bat_path)

    assert launcher.calls[0]["kwargs"]["creationflags"] == 0


def test_start_twice_raises(tmp_path: Path):
    bat_path = tmp_path / "General.bat"
    bat_path.write_text("@echo off\n")
    zapret = ZapretProcess(popen=FakeLauncher(), runner=FakeRunner(), allowed_root=tmp_path)

    zapret.start(bat_path)

    with pytest.raises(RuntimeError):
        zapret.start(bat_path)


def test_stop_when_never_started_touches_nothing(tmp_path: Path):
    runner = FakeRunner()
    zapret = ZapretProcess(popen=FakeLauncher(), runner=runner, allowed_root=tmp_path)

    zapret.stop()

    assert runner.calls == []
    assert zapret.is_running is False


def test_stop_kills_only_the_tracked_process_tree(tmp_path: Path):
    """/T, and nothing else.

    /T because the tracked pid is the .bat's cmd.exe host and winws.exe is its
    child - killing the pid alone orphans winws with its WinDivert handle
    still open. "Nothing else" because stop() used to follow up with a blanket
    `taskkill /F /IM winws.exe`, which reached every winws on the machine
    including ones BridgeBox never launched."""
    bat_path = tmp_path / "General.bat"
    bat_path.write_text("@echo off\n")
    runner = FakeRunner()
    zapret = ZapretProcess(popen=FakeLauncher(pid=4242), runner=runner, allowed_root=tmp_path)
    zapret.start(bat_path)

    zapret.stop()

    assert runner.calls == [["taskkill", "/F", "/T", "/PID", "4242"]]
    assert zapret.is_running is False


def test_stop_does_not_taskkill_a_process_that_already_exited(tmp_path: Path):
    """A pid whose process is gone is a number Windows may have handed to
    somebody else - and /T would take that process's children with it."""
    bat_path = tmp_path / "General.bat"
    bat_path.write_text("@echo off\n")
    runner = FakeRunner()
    zapret = ZapretProcess(
        popen=FakeLauncher(pid=4242, exit_code=0), runner=runner, allowed_root=tmp_path
    )
    zapret.start(bat_path)

    zapret.stop()

    assert runner.calls == []
    assert zapret.is_running is False


def test_kill_all_winws_is_only_the_name_sweep(tmp_path: Path):
    """The machine-wide sweep still exists, but as its own alarming name with
    one justified caller (the updater, which is about to overwrite the files
    a stray winws is holding)."""
    runner = FakeRunner()

    kill_all_winws(runner)

    assert runner.calls == [["taskkill", "/F", "/IM", "winws.exe"]]


def test_stop_is_idempotent(tmp_path: Path):
    bat_path = tmp_path / "General.bat"
    bat_path.write_text("@echo off\n")
    runner = FakeRunner()
    zapret = ZapretProcess(popen=FakeLauncher(), runner=runner, allowed_root=tmp_path)
    zapret.start(bat_path)

    zapret.stop()
    calls_after_first_stop = len(runner.calls)
    zapret.stop()

    assert len(runner.calls) == calls_after_first_stop


def test_refuses_to_execute_a_bat_outside_the_allowed_root(tmp_path: Path):
    """This is the one line in the app that executes a file, in a process
    main() requires to be elevated. BridgeBox ships portable, so config.yaml
    sits in a user-writable folder next to the binary - without this, editing
    one line of YAML to point zapret.dir at an attacker-controlled directory
    is local privilege escalation."""
    outside = tmp_path / "evil"
    outside.mkdir()
    payload = outside / "General.bat"
    payload.write_text("@echo pwned", encoding="utf-8")

    install = tmp_path / "install"
    install.mkdir()
    launcher = FakeLauncher()
    zapret = ZapretProcess(popen=launcher, runner=FakeRunner(), allowed_root=install)

    with pytest.raises(ValueError, match="escapes"):
        zapret.start(payload)

    assert launcher.calls == []
    assert zapret.is_running is False


def test_refuses_a_traversal_path_that_lands_outside(tmp_path: Path):
    install = tmp_path / "install"
    (install / "strategies").mkdir(parents=True)
    (tmp_path / "outside.bat").write_text("@echo pwned", encoding="utf-8")
    launcher = FakeLauncher()
    zapret = ZapretProcess(popen=launcher, runner=FakeRunner(), allowed_root=install)

    with pytest.raises(ValueError, match="escapes"):
        zapret.start(install / "strategies" / ".." / ".." / "outside.bat")

    assert launcher.calls == []


def test_refuses_a_non_bat_file_inside_the_root(tmp_path: Path):
    """Only .bat is ever a legitimate strategy - anything else reaching Popen
    means the path came from somewhere it should not have."""
    install = tmp_path / "install"
    install.mkdir()
    payload = install / "payload.exe"
    payload.write_text("stub", encoding="utf-8")
    launcher = FakeLauncher()
    zapret = ZapretProcess(popen=launcher, runner=FakeRunner(), allowed_root=install)

    with pytest.raises(ValueError, match=r"\.bat"):
        zapret.start(payload)

    assert launcher.calls == []


def test_stop_survives_a_taskkill_that_hangs(tmp_path):
    """Live symptom: the log ended at "stopping zapret" with no "zapret
    stopped", winws.exe was gone, and the UI toggle froze. stop() runs on the
    event loop thread, so a taskkill that never returns blocks everything.

    Killing is best-effort anyway - the Job Object is the real guarantee - so
    a timeout must be survivable rather than fatal."""
    import subprocess

    strategies = tmp_path / "strategies"
    strategies.mkdir()
    bat = strategies / "General.bat"
    bat.write_text("@echo off\n", encoding="utf-8")

    class _Handle:
        pid = 4242

        def poll(self):
            return None  # still running, so stop() really does reach taskkill

    calls = []

    def hanging_runner(cmd, **kwargs):
        calls.append((cmd, kwargs))
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

    process = ZapretProcess(
        popen=lambda cmd, **kw: _Handle(), runner=hanging_runner, allowed_root=tmp_path
    )
    process.start(bat)

    process.stop()  # must return, not hang and not raise

    assert not process.is_running, "a timed-out kill must still clear the handle"
    assert all("timeout" in kwargs for _, kwargs in calls), (
        "every taskkill needs a timeout - without one subprocess.run blocks forever"
    )


class _Tasklist:
    """Fakes `tasklist /FI IMAGENAME eq winws.exe`, alive for N calls."""

    def __init__(self, alive_for: int):
        self.alive_for = alive_for
        self.calls = 0

    def __call__(self, cmd, **kwargs):
        self.calls += 1
        alive = self.calls <= self.alive_for

        class Result:
            returncode = 0
            stdout = b"winws.exe   4242 Console  1  12 345 K" if alive else b"INFO: No tasks are running."

        return Result()


def test_wait_for_winws_exit_polls_until_the_process_is_really_gone():
    """taskkill returns once it has ASKED the kernel to terminate, not once the
    process is gone - and the file replacement that follows was starting while
    winws was still being torn down, which is the "[WinError 5] Отказано в
    доступе" on WinDivert64.sys."""
    runner = _Tasklist(alive_for=3)
    slept = []

    gone = wait_for_winws_exit(runner=runner, sleep=slept.append, monotonic=lambda: 0.0)

    assert gone is True
    assert runner.calls == 4, "polled until the answer changed"
    assert len(slept) == 3


def test_wait_for_winws_exit_gives_up_rather_than_letting_the_replace_start():
    """Returning True on timeout would hand the updater a green light to
    overwrite files something is still holding - a half-applied zapret."""
    clock = iter([0.0, 5.0, 20.0])
    runner = _Tasklist(alive_for=999)

    gone = wait_for_winws_exit(
        runner=runner, sleep=lambda _: None, monotonic=lambda: next(clock)
    )

    assert gone is False


def test_an_unanswerable_tasklist_counts_as_still_running():
    """The callers use this to decide whether it is safe to overwrite files.
    Guessing "gone" when we cannot tell is what produces the half-applied
    state; guessing "running" only costs a refused update."""

    def exploding(cmd, **kwargs):
        raise OSError("tasklist missing")

    assert winws_is_running(exploding) is True


def test_every_console_helper_runs_without_a_window():
    """A flashed cmd window is what "Скрывать консоль" is supposed to prevent,
    and it has to hold for the helpers too, not just for winws itself."""
    calls = []

    def recording(cmd, **kwargs):
        calls.append(kwargs.get("creationflags"))

        class Result:
            returncode = 0
            stdout = b""

        return Result()

    kill_all_winws(recording)
    winws_is_running(recording)

    assert calls and all(flag == NO_WINDOW for flag in calls)


# ---- the console, and noticing it being closed ----


class WatchablePopen(FakePopenResult):
    """Adds the two things the watchdog and the log pump need: a wait() that
    blocks until the test releases it, and an optional output stream."""

    def __init__(self, pid: int = 4242, output: bytes = b""):
        super().__init__(pid, exit_code=None)
        self.exit_code_after_wait = 1
        self._released = threading.Event()
        self.stdout = io.BytesIO(output) if output else None

    def release(self, code: int = 1) -> None:
        self.exit_code_after_wait = code
        self._released.set()

    def wait(self):
        self._released.wait(timeout=5)
        self._exit_code = self.exit_code_after_wait
        return self.exit_code_after_wait


class WatchableLauncher:
    def __init__(self, process):
        self.process = process
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": cmd, "kwargs": kwargs})
        return self.process


def test_hiding_the_console_is_not_the_same_flag_as_showing_it():
    """0 is not "show it": under pythonw there is no console to inherit, so a
    child launched with no flags gets none at all - which is why turning the
    setting off appeared to do nothing."""
    assert console_flags(True) == NO_WINDOW
    assert console_flags(False) == NEW_CONSOLE
    assert NEW_CONSOLE != 0


def test_an_unexpected_exit_reaches_the_handler(tmp_path: Path):
    """Closing the winws console by hand kills the batch tree. Nobody asked
    for that, so the bridge has to be told."""
    bat_path = tmp_path / "General.bat"
    bat_path.write_text("@echo off\n")
    process = WatchablePopen()
    zapret = ZapretProcess(
        popen=WatchableLauncher(process), runner=FakeRunner(), allowed_root=tmp_path
    )
    seen = []
    zapret.start(bat_path, on_exit=seen.append)

    process.release(code=3)
    deadline = time.monotonic() + 5
    while not seen and time.monotonic() < deadline:
        time.sleep(0.01)

    assert seen == [3]
    assert zapret.is_running is False


def test_stopping_on_purpose_does_not_look_like_a_crash(tmp_path: Path):
    """stop() clears the handle before the process actually dies, and that is
    the whole test for "did we ask for this" - without it every normal stop
    would raise the console-was-closed notice."""
    bat_path = tmp_path / "General.bat"
    bat_path.write_text("@echo off\n")
    process = WatchablePopen()
    zapret = ZapretProcess(
        popen=WatchableLauncher(process), runner=FakeRunner(), allowed_root=tmp_path
    )
    seen = []
    zapret.start(bat_path, on_exit=seen.append)

    zapret.stop()
    process.release(code=0)
    time.sleep(0.1)

    assert seen == []


def test_a_hidden_console_still_reaches_the_log(tmp_path: Path, caplog):
    """Hidden used to mean lost: everything winws printed - including why it
    refused to start - went to a console nobody has."""
    bat_path = tmp_path / "General.bat"
    bat_path.write_text("@echo off\n")
    process = WatchablePopen(output="winws: filter loaded\n".encode("cp866"))
    launcher = WatchableLauncher(process)
    zapret = ZapretProcess(popen=launcher, runner=FakeRunner(), allowed_root=tmp_path)

    with caplog.at_level(logging.INFO, logger="bridgebox.zapret.console"):
        zapret.start(bat_path, creationflags=NO_WINDOW, capture_output=True)
        deadline = time.monotonic() + 5
        while "filter loaded" not in caplog.text and time.monotonic() < deadline:
            time.sleep(0.01)

    assert launcher.calls[0]["kwargs"]["stdout"] is subprocess.PIPE
    assert "winws: filter loaded" in caplog.text


def test_a_visible_console_keeps_its_own_output(tmp_path: Path):
    """Piping the output away would empty the very window the user asked to
    see, so capture and a visible console are mutually exclusive by design."""
    bat_path = tmp_path / "General.bat"
    bat_path.write_text("@echo off\n")
    launcher = FakeLauncher()
    zapret = ZapretProcess(popen=launcher, runner=FakeRunner(), allowed_root=tmp_path)

    zapret.start(bat_path, creationflags=console_flags(False), capture_output=False)

    assert "stdout" not in launcher.calls[0]["kwargs"]


def test_the_driver_is_stopped_never_deleted():
    """`sc delete` on a loaded driver leaves it pending-delete, which makes
    WinDivert64.sys MORE locked until a reboot - the opposite of the point."""
    runner = FakeRunner()

    stop_windivert_service(runner)

    assert runner.calls, "no service command was issued at all"
    assert all(cmd[:2] == ["sc", "stop"] for cmd in runner.calls)
    assert not any("delete" in cmd for cmd in runner.calls)


def test_a_deliberate_stop_does_not_look_like_a_crash_to_the_watchdog(tmp_path: Path):
    """The regression. taskkill makes process.wait() return in milliseconds,
    and the handle used to be cleared AFTER the kill - so the watchdog checked
    "did anybody ask for this" while the handle was still set and lost the
    race. The real log shows it: "stopping zapret: pid=27040" followed 135ms
    later by "zapret exited on its own (pid=27040)".

    Two things went wrong every single stop: the user got the
    console-was-closed notice, and a SECOND concurrent bridge teardown was
    submitted on top of the one already running."""
    bat_path = tmp_path / "General.bat"
    bat_path.write_text("@echo off\n")
    process = WatchablePopen()
    seen = []

    class KillingRunner:
        """taskkill that actually kills, the way the real one does - including
        taking time to return.

        The delay is the whole test. Without it the fake is faster than the
        watchdog thread can wake, stop() reaches its last line first, and the
        race never happens - which is how the first version of this test passed
        against the bug it was written to catch. The real taskkill took 135ms
        in the log this reproduces."""

        def __init__(self):
            self.calls = []

        def __call__(self, cmd, **kwargs):
            self.calls.append(cmd)
            process.release(code=1)  # dies while stop() is still inside taskkill
            time.sleep(0.15)  # ...and the watchdog gets to run during that

            class Result:
                returncode = 0

            return Result()

    zapret = ZapretProcess(
        popen=WatchableLauncher(process), runner=KillingRunner(), allowed_root=tmp_path
    )
    zapret.start(bat_path, on_exit=seen.append)

    zapret.stop()
    time.sleep(0.2)  # give the watchdog every chance to fire

    assert seen == [], "the watchdog reported a deliberate stop as a crash"
    assert zapret.is_running is False
