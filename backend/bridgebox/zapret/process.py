from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..paths import PROJECT_ROOT
from .job import ProcessJobObject

logger = logging.getLogger(__name__)

# winws's own output, kept on its own logger so the Logs screen's search can
# separate "what zapret said" from "what BridgeBox did about it".
console_logger = logging.getLogger("bridgebox.zapret.console")

# Bound on each taskkill. stop() runs on the event loop thread, so an
# unbounded wait freezes the UI - the zapret toggle stuck mid-flip is exactly
# how this surfaced. Generous enough that a healthy kill never trips it.
KILL_TIMEOUT_S = 10

# Every console helper this module shells out to (taskkill, tasklist) would
# otherwise flash a black window over the app. The app is meant to be one
# window; see also autostart.py, tls/ca.py and runtime_core.py.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# How long to keep asking Windows whether winws is really gone. taskkill
# returns as soon as it has *asked*, not once the process has died - and the
# WinDivert driver it holds unloads later still. Everything that replaces
# zapret's files depends on this wait, not on taskkill's exit code.
EXIT_WAIT_TIMEOUT_S = 15.0
EXIT_POLL_INTERVAL_S = 0.25

# The two ways zapret's console can be handled, and why neither is 0.
#
# CREATE_NO_WINDOW hides it. CREATE_NEW_CONSOLE gives it a real window of its
# own - which is NOT what 0 does once BridgeBox runs under pythonw.exe: a
# pythonw process has no console at all, so a child launched with no flags
# inherits nothing and its output goes nowhere visible. That is why turning
# "Скрывать консоль" off appeared to do nothing.
NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010)

# Service names WinDivert has registered across versions. winws creates the
# service itself and normally removes it on exit, but a kill leaves it loaded,
# and a loaded driver is what holds WinDivert64.sys open.
WINDIVERT_SERVICES = ("WinDivert", "WinDivert1.4", "windivert")


def console_flags(hide: bool) -> int:
    """Creation flags for winws's console, from the `hide_console` setting.

    One function rather than a conditional at the call site, because the wrong
    half of it is invisible until somebody runs the app the way users do (under
    pythonw, where 0 means "no console anywhere" rather than "inherit ours")."""
    return NO_WINDOW if hide else NEW_CONSOLE


def stop_windivert_service(runner: Runner = subprocess.run) -> bool:
    """Ask Windows to unload the WinDivert driver. Best-effort.

    `sc stop`, never `sc delete`: deleting a service whose driver is still
    loaded marks it pending-delete, which leaves the .sys file MORE locked
    until a reboot - the opposite of what the caller (the updater) wants.

    Returns whether any of the known names reported success. False is not an
    error: winws removes its own service on a clean exit, so "no such service"
    is the normal case and means the driver is already gone."""
    stopped = False
    for name in WINDIVERT_SERVICES:
        try:
            result = runner(
                ["sc", "stop", name],
                capture_output=True,
                timeout=KILL_TIMEOUT_S,
                creationflags=NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("could not stop the %s service: %s", name, exc)
            continue
        if getattr(result, "returncode", 1) == 0:
            logger.info("asked the %s service to stop before replacing zapret files", name)
            stopped = True
    return stopped


class Launcher(Protocol):
    def __call__(self, cmd: list[str], **kwargs: Any) -> Any: ...


class Runner(Protocol):
    def __call__(self, cmd: list[str], **kwargs: Any) -> Any: ...


class JobAssigner(Protocol):
    def assign(self, pid: int) -> bool: ...


@dataclass
class _Handle:
    pid: int
    # The Popen object, kept rather than discarded. Two jobs, both load-bearing:
    # poll() tells stop() whether the process is still alive, and simply holding
    # it keeps the OS process handle open, which is what stops Windows from
    # recycling the pid. Without that, a `taskkill /T` aimed at a pid this
    # session started could land on an unrelated process that inherited the
    # number - and /T would take its children with it.
    process: Any


def _text(value: Any) -> str:
    """taskkill output is bytes in production and str/None in tests."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value).strip() if value else ""


def _decode_console(raw: Any) -> str:
    """Windows console output, in whichever encoding it really is.

    cp866 before utf-8 because that is what a Russian Windows console writes;
    tests hand this str, which passes straight through."""
    if not isinstance(raw, bytes):
        return str(raw)
    for encoding in ("cp866", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _taskkill(runner: Runner, *args: str):
    """Best-effort, and bounded.

    Without a timeout subprocess.run waits forever, and stop() runs on the
    event loop thread - so a taskkill that does not return froze the whole UI,
    leaving the zapret toggle stuck mid-flip with the log ending at "stopping
    zapret". Observed live once winws.exe held a WinDivert handle over a wider
    filter.

    Killing is best effort anyway: the kill-on-close Job Object is what
    actually guarantees winws.exe dies, so a timeout here is worth a warning,
    not an exception."""
    try:
        return runner(
            ["taskkill", "/F", *args],
            capture_output=True,
            timeout=KILL_TIMEOUT_S,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "taskkill %s did not return within %ss - relying on the job object",
            " ".join(args),
            KILL_TIMEOUT_S,
        )
        return None


def winws_is_running(runner: Runner = subprocess.run) -> bool:
    """Ask Windows whether any winws.exe still exists.

    tasklist rather than psutil: psutil is not a dependency and this is one
    question asked a few times per update. `tasklist /FI` prints a plain
    "INFO: No tasks..." line when nothing matches, so the image name appearing
    in the output IS the answer."""
    try:
        result = runner(
            ["tasklist", "/FI", "IMAGENAME eq winws.exe", "/NH"],
            capture_output=True,
            timeout=KILL_TIMEOUT_S,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Unknowable is treated as "still running": the callers use this to
        # decide whether it is safe to overwrite files, and guessing "gone"
        # there is what produces a half-applied update.
        logger.warning("could not query tasklist for winws.exe: %s", exc)
        return True
    return b"winws.exe" in bytes(getattr(result, "stdout", b"") or b"").lower() or (
        "winws.exe" in str(getattr(result, "stdout", "") or "").lower()
    )


def wait_for_winws_exit(
    *,
    runner: Runner = subprocess.run,
    timeout_s: float = EXIT_WAIT_TIMEOUT_S,
    interval_s: float = EXIT_POLL_INTERVAL_S,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> bool:
    """Block until no winws.exe is left, or the timeout runs out.

    This is the step the updater was missing. `taskkill /F` returns once it has
    asked the kernel to terminate, so the very next line of code could - and
    did - try to rename WinDivert64.sys while the process holding it was still
    being torn down, producing "[WinError 5] Отказано в доступе".

    Returns True if the process is genuinely gone. False means the caller
    should refuse to touch the files rather than fail halfway."""
    deadline = monotonic() + timeout_s
    while True:
        if not winws_is_running(runner):
            return True
        if monotonic() >= deadline:
            logger.error("winws.exe still present after %.1fs - not safe to replace files", timeout_s)
            return False
        sleep(interval_s)


def kill_all_winws(runner: Runner = subprocess.run) -> None:
    """Kill EVERY winws.exe on the machine, including ones BridgeBox never
    started.

    Deliberately a module function with an alarming name rather than part of
    ZapretProcess.stop(), which used to do this on every single stop. Only one
    caller is justified: the updater, which is about to overwrite winws.exe and
    WinDivert64.sys and cannot proceed while any process holds them - and a
    winws left over from a previous BridgeBox session is invisible to
    ZapretProcess (see is_running). Anywhere else, use stop()."""
    result = _taskkill(runner, "/IM", "winws.exe")
    if result is not None:
        logger.info(
            "swept every winws.exe before replacing zapret binaries: rc=%s out=%r",
            getattr(result, "returncode", "?"),
            _text(getattr(result, "stdout", b"")),
        )


class ZapretProcess:
    """Manages the winws.exe lifecycle via the BridgeBox-adapted strategy
    .bat files in zapret/strategies/ (see zapret/README.md).

    Runs the .bat in the foreground (no `start`) so the PID is trackable.

    stop() is scoped to what this session started, and nothing else. Two
    things make that true, and both matter:

      - The tracked pid is the .bat's `cmd.exe` host, NOT winws.exe - the
        strategy file runs winws as its last statement, so winws is a *child*.
        The kill is therefore `taskkill /F /T /PID`: without /T the batch host
        dies and winws is orphaned, still holding its WinDivert handle.
      - _Handle keeps the Popen object, so the pid cannot be recycled out from
        under us and poll() can say whether there is anything left to kill.

    stop() used to also run a blanket `taskkill /F /IM winws.exe`, which
    reached every winws on the machine including ones BridgeBox never
    launched. That now lives in kill_all_winws() with one justified caller,
    the updater.

    Assumes the whole BridgeBox process is already elevated (per PRD:
    mandatory admin launch), so no UAC prompt is triggered here.

    job assigns the launched process to a Windows Job Object with kill-on-
    close set (see zapret/job.py), so winws.exe dies with BridgeBox even on
    a hard kill/crash where no Python cleanup code runs at all - graceful
    stop() already handles the normal-exit case via taskkill."""

    def __init__(
        self,
        *,
        popen: Launcher = subprocess.Popen,
        runner: Runner = subprocess.run,
        job: JobAssigner | None = None,
        allowed_root: Path | None = None,
    ):
        self._popen = popen
        self._runner = runner
        self._job = job if job is not None else ProcessJobObject()
        self._handle: _Handle | None = None
        # Nothing outside this tree may be executed - see start(). Injected
        # rather than read from paths.PROJECT_ROOT directly so the boundary is
        # a property of the instance (and testable), the same way popen/runner/
        # job already are.
        self._allowed_root = (allowed_root or PROJECT_ROOT).resolve()

    @property
    def is_running(self) -> bool:
        return self._handle is not None

    def start(
        self,
        bat_path: str | Path,
        *,
        creationflags: int = 0,
        on_exit=None,
        capture_output: bool = False,
    ) -> int:
        if self._handle is not None:
            logger.error(
                "refusing to start %s: zapret already running (pid=%s)",
                bat_path,
                self._handle.pid,
            )
            raise RuntimeError("zapret is already running")

        bat_path = Path(bat_path)

        # Defence in depth behind ZapretConfig's own validator: this is the
        # single line in the app that executes a file, in a process running as
        # Administrator, so it does not delegate the check to whoever resolved
        # the path. A future caller that builds a path some other way must not
        # be able to turn this into arbitrary elevated execution.
        resolved = bat_path.resolve()
        if not resolved.is_relative_to(self._allowed_root):
            logger.error("refusing to execute a strategy outside the install: %s", resolved)
            raise ValueError(f"strategy path escapes the BridgeBox folder: {resolved}")
        if resolved.suffix.lower() != ".bat":
            logger.error("refusing to execute a non-.bat strategy: %s", resolved)
            raise ValueError(f"strategy must be a .bat file: {resolved}")

        if not bat_path.exists():
            # Logged rather than raised here: Popen's own error is clearer,
            # but it doesn't say which strategy was being resolved.
            logger.error("strategy .bat does not exist: %s", bat_path)
        logger.info("starting zapret: %s (cwd=%s)", bat_path.name, bat_path.parent)
        logger.debug(
            "zapret launch detail: cmd=%r cwd=%s creationflags=%#x",
            [str(bat_path)],
            bat_path.parent,
            creationflags,
        )
        popen_kwargs: dict[str, Any] = {
            "cwd": str(bat_path.parent),
            "creationflags": creationflags,
        }
        if capture_output:
            # Only meaningful with the console hidden. With a console of its
            # own the output belongs on screen, and piping it away would empty
            # the very window the user asked to see.
            popen_kwargs["stdout"] = subprocess.PIPE
            popen_kwargs["stderr"] = subprocess.STDOUT
        try:
            process = self._popen([str(bat_path)], **popen_kwargs)
        except Exception:
            logger.exception("failed to launch zapret from %s", bat_path)
            raise

        if capture_output and getattr(process, "stdout", None) is not None:
            self._pump(process)

        assigned = self._job.assign(process.pid)
        if not assigned:
            # Best-effort by design (see job.py) - but if it failed, winws.exe
            # can outlive a hard kill of BridgeBox, which is worth knowing.
            logger.warning(
                "pid %s not assigned to the kill-on-close job object - "
                "winws.exe may survive a crash",
                process.pid,
            )
        handle = _Handle(pid=process.pid, process=process)
        self._handle = handle
        logger.info("zapret started: pid=%s strategy=%s", process.pid, bat_path.name)
        if on_exit is not None:
            self._watch(handle, on_exit)
        return process.pid

    def _pump(self, process) -> None:
        """Copy winws's hidden console into the app log, line by line.

        With hide_console on, everything winws prints - including the reason it
        refused to start - went to a console nobody has and was lost. It now
        lands in the Logs screen with everything else.

        Decoded as cp866 first: winws writes through the Windows console, which
        is OEM-encoded on a Russian machine, and utf-8 there produces mojibake
        rather than an error you would notice."""

        def read() -> None:
            try:
                with process.stdout as stream:
                    for raw in iter(stream.readline, b""):
                        text = _decode_console(raw).rstrip()
                        if text:
                            console_logger.info("%s", text)
            except Exception:
                logger.debug("stopped reading zapret's output", exc_info=True)

        threading.Thread(target=read, name="zapret-console", daemon=True).start()

    def _watch(self, handle: _Handle, on_exit) -> None:
        """Notice winws dying without us asking it to.

        With the console visible (hide_console off) the user can close that
        window, which kills the whole batch tree - and BridgeBox went on
        reporting a running bridge with no bypass behind it. A thread blocked
        on wait() rather than a poll loop: the process object is already here,
        and this costs nothing while nothing happens.

        `self._handle is handle` is the whole "was this expected" test. stop()
        clears the handle before the process dies, and start() replaces it, so
        an exit that arrives while our handle is still the current one is the
        only one nobody asked for."""

        def wait() -> None:
            try:
                code = handle.process.wait()
            except Exception:
                logger.exception("the zapret watchdog stopped watching pid=%s", handle.pid)
                return
            if self._handle is not handle:
                return  # stop() or a restart got there first - expected
            logger.warning("zapret exited on its own (pid=%s, code=%s)", handle.pid, code)
            self._handle = None
            try:
                on_exit(code)
            except Exception:
                logger.exception("the zapret exit handler failed")

        threading.Thread(target=wait, name=f"zapret-watchdog-{handle.pid}", daemon=True).start()

    def stop(self) -> None:
        if self._handle is None:
            logger.debug("zapret stop requested but nothing was started by this session")
            return

        # Cleared BEFORE the kill, not after, and that ordering is the whole
        # fix for a bug this shipped with: the watchdog decides "did anybody
        # ask for this" by testing `self._handle is handle`, and taskkill makes
        # process.wait() return in milliseconds. With the handle cleared
        # afterwards, every deliberate stop raced the watchdog and lost - the
        # log shows "stopping zapret: pid=27040" followed 135ms later by
        # "zapret exited on its own (pid=27040)". That fired the
        # console-was-closed notice on a normal stop AND submitted a second,
        # concurrent bridge teardown.
        handle, self._handle = self._handle, None
        pid = handle.pid

        # Already gone - the .bat exited, or the job object reaped it. Killing
        # by pid here would be aimed at a number the OS is free to have handed
        # to somebody else, and /T would take that process's children too.
        if handle.process.poll() is not None:
            logger.info("zapret already exited on its own (pid=%s)", pid)
            return

        logger.info("stopping zapret: pid=%s", pid)
        # /T is what makes this correct rather than merely scoped: the tracked
        # pid is the .bat's cmd.exe host and winws.exe is its child, so killing
        # the pid alone leaves winws running with its WinDivert handle open.
        result = _taskkill(self._runner, "/T", "/PID", str(pid))
        if result is not None:
            # taskkill returns 128 when there was nothing to kill, which is a
            # normal outcome here, not a failure - log it without alarm.
            logger.debug(
                "taskkill tree -> rc=%s out=%r err=%r",
                getattr(result, "returncode", "?"),
                _text(getattr(result, "stdout", b"")),
                _text(getattr(result, "stderr", b"")),
            )
        logger.info("zapret stopped (pid=%s)", pid)
