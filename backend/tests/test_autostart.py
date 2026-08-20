"""Starting with Windows.

The load-bearing facts these tests pin: the task must be registered with
RunLevel HighestAvailable (main() exits outright without Administrator, so an
autostart that launches unelevated does not "mostly work" - it does not run
at all) and at a raised Task Scheduler priority (the whole point of asking
for this over a plain Run-key entry - see TASK_PRIORITY's comment).
"""
import subprocess
import sys
from pathlib import Path

from bridgebox import autostart


class FakeRunner:
    def __init__(self, returncode: int = 0):
        self.calls: list[list[str]] = []
        self.returncode = returncode
        # The XML content at the moment schtasks would have read it - the
        # real file is deleted right after _run() returns, so this is the
        # only place a test can still see it.
        self.xml_snapshots: list[str] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        self.kwargs = kwargs
        if "/XML" in cmd:
            xml_path = Path(cmd[cmd.index("/XML") + 1])
            self.xml_snapshots.append(xml_path.read_text(encoding="utf-16"))

        class Result:
            returncode = self.returncode
            stdout = b""
            stderr = b""

        return Result()


def test_the_task_is_registered_via_xml_with_the_highest_run_level():
    """/Create /XML, not the flag-only form - Task Scheduler has no /RL-style
    flag for Priority, so raising it means going through an XML definition,
    and RunLevel/LogonTrigger move into that same file."""
    runner = FakeRunner()

    assert autostart.enable(runner=runner) is True

    argv = runner.calls[0]
    assert argv[0] == "schtasks"
    assert argv[1] == "/Create"
    assert "/XML" in argv
    # /F, so toggling it on twice replaces rather than fails.
    assert "/F" in argv

    xml = runner.xml_snapshots[0]
    assert "<RunLevel>HighestAvailable</RunLevel>" in xml
    assert "<LogonTrigger>" in xml


def test_the_task_is_registered_at_a_raised_priority():
    """The reason this exists at all: BridgeBox must not sit waiting behind a
    logon-time pile of other startup programs for a CPU slice while zapret is
    still down."""
    runner = FakeRunner()

    autostart.enable(runner=runner)

    assert f"<Priority>{autostart.TASK_PRIORITY}</Priority>" in runner.xml_snapshots[0]


def test_a_custom_priority_is_honoured():
    runner = FakeRunner()

    autostart.enable(priority=2, runner=runner)

    assert "<Priority>2</Priority>" in runner.xml_snapshots[0]


def test_the_minimized_variant_passes_the_flag_the_app_reads_back():
    runner = FakeRunner()

    autostart.enable(minimized=True, runner=runner)

    assert "<Arguments>-m bridgebox.desktop --minimized</Arguments>" in runner.xml_snapshots[0]
    # The other half of the contract: main() has to recognise it.
    assert autostart.started_minimized(["prog", "--minimized"]) is True
    assert autostart.started_minimized(["prog"]) is False


def test_the_temp_xml_file_is_cleaned_up_after_create():
    """A leftover .xml in %TEMP% on every toggle is exactly the kind of
    thing that piles up silently for months."""
    seen_paths: list[Path] = []

    def runner(cmd, **kwargs):
        if "/XML" in cmd:
            seen_paths.append(Path(cmd[cmd.index("/XML") + 1]))

        class Result:
            returncode = 0

        return Result()

    autostart.enable(runner=runner)

    assert seen_paths and not seen_paths[0].exists()


def test_the_launch_command_keeps_the_executable_path_intact():
    """A path like "C:\\Program Files\\..." must reach the XML's <Command>
    element whole - unlike a schtasks /TR flag, an XML element has no shell
    to quote-split for."""
    command, _arguments = autostart._launch_command_parts(minimized=False)

    assert command == sys.executable


def test_a_refused_schtasks_reports_failure_instead_of_raising():
    """Autostart is a convenience. A locked-down machine must leave a working
    app behind, with the toggle telling the truth."""
    assert autostart.enable(runner=FakeRunner(returncode=1)) is False


def test_a_missing_schtasks_is_just_false():
    def exploding(cmd, **kwargs):
        raise FileNotFoundError("schtasks")

    assert autostart.is_enabled(runner=exploding) is False
    assert autostart.enable(runner=exploding) is False
    assert autostart.disable(runner=exploding) is False


def test_a_hanging_schtasks_cannot_hang_the_settings_toggle():
    def hanging(cmd, **kwargs):
        assert "timeout" in kwargs, "every schtasks call must be bounded"
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    assert autostart.enable(runner=hanging) is False


def test_deleting_a_task_that_was_never_there_counts_as_success():
    """The user asked for "not starting with Windows", and that is already
    true - reporting failure would be a lie that shows a red error."""
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)

        class Result:
            # schtasks returns non-zero both for "refused" and "no such task";
            # is_enabled is what tells the two apart.
            returncode = 1

        return Result()

    assert autostart.disable(runner=runner) is True
    assert any("/Delete" in cmd for cmd in calls)
    assert any("/Query" in cmd for cmd in calls)


def test_no_console_window_flashes_on_any_call():
    """Every schtasks call runs behind the UI. Without CREATE_NO_WINDOW each
    one flashes a console over the app, which reads as something crashing."""
    runner = FakeRunner()

    autostart.is_enabled(runner=runner)

    assert runner.kwargs.get("creationflags") == getattr(subprocess, "CREATE_NO_WINDOW", 0)
