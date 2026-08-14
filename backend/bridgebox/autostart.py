"""Start BridgeBox with Windows.

A Task Scheduler task, not the HKCU\\...\\Run key everything else uses. That is
forced, not a preference: main() exits outright without Administrator (zapret
needs WinDivert, which needs a kernel driver), so a Run entry would either die
at every boot or raise a UAC prompt every single time the machine starts. A
task registered with RunLevel=HIGHEST launches elevated and silently.

schtasks.exe rather than the COM Task Scheduler API: two commands, no
dependency, and the XML the COM path would need is longer than this file.

Everything here is best-effort and returns a bool. Autostart failing must
never stop the app from running - it is a convenience, and the user is told
through the returned value rather than an exception.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Visible in Task Scheduler under this name, so somebody who finds it there
# knows what it is and can delete it by hand.
TASK_NAME = "BridgeBox Autostart"

# schtasks is not a hot path; a task that hangs must not hang the settings
# toggle that called it.
TIMEOUT_S = 15

# CREATE_NO_WINDOW: without it every schtasks call flashes a console window
# over the app, which looks exactly like something crashing.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def launch_command(minimized: bool) -> str:
    """The command line the task runs.

    Frozen-build aware in the same way restart_app already is: a PyInstaller
    build has no interpreter to hand a module name to, so `sys.executable` IS
    the app. Quoted because Program Files exists."""
    if getattr(sys, "frozen", False):
        command = f'"{sys.executable}"'
    else:
        command = f'"{sys.executable}" -m bridgebox.desktop'
    return f"{command} --minimized" if minimized else command


def _run(args: list[str], *, runner=subprocess.run):
    return runner(
        ["schtasks", *args],
        capture_output=True,
        timeout=TIMEOUT_S,
        creationflags=_NO_WINDOW,
    )


def is_enabled(*, runner=subprocess.run) -> bool:
    """Whether the task exists. Never raises - a missing schtasks.exe, a
    locked-down machine and "no such task" are all just False."""
    try:
        result = _run(["/Query", "/TN", TASK_NAME], runner=runner)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("could not query the autostart task: %s", exc)
        return False
    return getattr(result, "returncode", 1) == 0


def enable(*, minimized: bool = False, runner=subprocess.run) -> bool:
    """Create (or replace) the logon task. Returns whether it now exists."""
    args = [
        "/Create",
        "/TN", TASK_NAME,
        "/TR", launch_command(minimized),
        "/SC", "ONLOGON",
        # The whole reason this is a task and not a Run key.
        "/RL", "HIGHEST",
        # Replace rather than fail when it already exists - the toggle is not
        # a state machine, it just says what the user wants now.
        "/F",
    ]
    try:
        result = _run(args, runner=runner)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("could not create the autostart task: %s", exc)
        return False
    ok = getattr(result, "returncode", 1) == 0
    if ok:
        logger.info("autostart task created (minimized=%s)", minimized)
    else:
        logger.error(
            "schtasks refused to create the autostart task: rc=%s %s",
            getattr(result, "returncode", "?"),
            _text(getattr(result, "stderr", b"")),
        )
    return ok


def disable(*, runner=subprocess.run) -> bool:
    """Remove the task. A task that was never there counts as success - the
    user asked for "not starting with Windows", and that is already true."""
    try:
        result = _run(["/Delete", "/TN", TASK_NAME, "/F"], runner=runner)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("could not delete the autostart task: %s", exc)
        return False
    if getattr(result, "returncode", 1) == 0:
        logger.info("autostart task removed")
        return True
    # schtasks returns non-zero for "does not exist", which is the state the
    # caller wanted anyway.
    return not is_enabled(runner=runner)


def _text(value) -> str:
    """schtasks writes OEM-encoded bytes on a localised Windows."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value).strip() if value else ""


def started_minimized(argv: list[str] | None = None) -> bool:
    """Whether this process was launched by the minimized autostart task."""
    return "--minimized" in (argv if argv is not None else sys.argv)


def task_scheduler_available() -> bool:
    """Whether schtasks.exe exists at all. Used to hide the setting rather
    than offer a toggle that silently does nothing."""
    return Path(sys.executable).exists() and _which_schtasks() is not None


def _which_schtasks() -> str | None:
    import shutil

    return shutil.which("schtasks")
