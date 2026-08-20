"""Start BridgeBox with Windows.

A Task Scheduler task, not the HKCU\\...\\Run key everything else uses. That is
forced, not a preference: main() exits outright without Administrator (zapret
needs WinDivert, which needs a kernel driver), so a Run entry would either die
at every boot or raise a UAC prompt every single time the machine starts. A
task registered with RunLevel HighestAvailable launches elevated and silently.

Registered via an XML definition (schtasks /Create /XML), not the flag-only
form (/TR /SC /RL): the flag form has no way to ask Task Scheduler to start
the process at a raised priority, and that is the other half of "priority
autostart" - a logon storm of a dozen other startup programs must not leave
BridgeBox waiting behind them for a CPU slice while zapret is still down.
schtasks.exe itself (not the COM Task Scheduler API) because it is already a
dependency-free way to talk to the scheduler; XML is just what /Create takes
when a setting isn't exposed as a flag.

Everything here is best-effort and returns a bool. Autostart failing must
never stop the app from running - it is a convenience, and the user is told
through the returned value rather than an exception.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

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

# Task Scheduler's Settings/Priority, 0 (highest) - 10 (lowest), default 7.
# It maps straight onto Win32 process priority classes: 0-1 Realtime, 2-3
# High, 4-5 Above Normal, 6-8 Normal, 9 Below Normal, 10 Idle. 4 (Above
# Normal) is the boost: enough that BridgeBox is not left waiting behind a
# logon-time pile of other startup programs for zapret to come up, without
# reaching into Realtime/High - those can starve the rest of the system and
# are reserved for things that would misbehave if delayed at all, which a
# network bridge is not.
TASK_PRIORITY = 4


def _launch_command_parts(minimized: bool) -> tuple[str, str]:
    """(command, arguments) for the task's <Exec> action.

    Frozen-build aware in the same way restart_app already is: a PyInstaller
    build has no interpreter to hand a module name to, so `sys.executable` IS
    the app."""
    if getattr(sys, "frozen", False):
        command, base_args = sys.executable, ""
    else:
        command, base_args = sys.executable, "-m bridgebox.desktop"
    arguments = f"{base_args} --minimized".strip() if minimized else base_args
    return command, arguments


def _task_xml(command: str, arguments: str, *, priority: int) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        "      <RunLevel>HighestAvailable</RunLevel>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        f"    <Priority>{priority}</Priority>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{_xml_escape(command)}</Command>\n"
        f"      <Arguments>{_xml_escape(arguments)}</Arguments>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


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


def enable(
    *, minimized: bool = False, priority: int = TASK_PRIORITY, runner=subprocess.run
) -> bool:
    """Create (or replace) the logon task. Returns whether it now exists."""
    command, arguments = _launch_command_parts(minimized)
    xml = _task_xml(command, arguments, priority=priority)
    # mkstemp + write + separate delete, not NamedTemporaryFile(delete=True):
    # the latter keeps its own handle open on Windows, and schtasks opening
    # the same path while we still hold it is a sharing violation waiting to
    # happen.
    fd, tmp_name = tempfile.mkstemp(suffix=".xml", prefix="bridgebox-autostart-")
    os.close(fd)
    tmp_path = Path(tmp_name)
    # UTF-16: what schtasks' own XML export produces, and the encoding it
    # reliably parses - a plain UTF-8 file has silently failed to import on
    # some Windows builds.
    tmp_path.write_text(xml, encoding="utf-16")
    try:
        result = _run(
            # Replace rather than fail when it already exists - the toggle is
            # not a state machine, it just says what the user wants now.
            ["/Create", "/TN", TASK_NAME, "/XML", str(tmp_path), "/F"],
            runner=runner,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("could not create the autostart task: %s", exc)
        return False
    finally:
        tmp_path.unlink(missing_ok=True)
    ok = getattr(result, "returncode", 1) == 0
    if ok:
        logger.info("autostart task created (minimized=%s, priority=%s)", minimized, priority)
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
