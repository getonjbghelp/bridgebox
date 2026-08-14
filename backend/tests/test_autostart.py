"""Starting with Windows.

The load-bearing fact these tests pin: the task must be registered with
/RL HIGHEST. main() exits outright without Administrator, so an autostart
that launches unelevated does not "mostly work" - it does not run at all.
"""
import subprocess
import sys

from bridgebox import autostart


class FakeRunner:
    def __init__(self, returncode: int = 0):
        self.calls: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        self.kwargs = kwargs

        class Result:
            returncode = self.returncode
            stdout = b""
            stderr = b""

        return Result()


def test_the_task_is_registered_with_the_highest_run_level():
    """Without /RL HIGHEST the task launches unelevated, main() hits its admin
    check and exits, and autostart silently does nothing every boot."""
    runner = FakeRunner()

    assert autostart.enable(runner=runner) is True

    argv = runner.calls[0]
    assert argv[0] == "schtasks"
    assert "/RL" in argv and argv[argv.index("/RL") + 1] == "HIGHEST"
    assert "/SC" in argv and argv[argv.index("/SC") + 1] == "ONLOGON"
    # /F, so toggling it on twice replaces rather than fails.
    assert "/F" in argv


def test_the_minimized_variant_passes_the_flag_the_app_reads_back():
    runner = FakeRunner()

    autostart.enable(minimized=True, runner=runner)

    command = runner.calls[0][runner.calls[0].index("/TR") + 1]
    assert command.endswith("--minimized")
    # The other half of the contract: main() has to recognise it.
    assert autostart.started_minimized(["prog", "--minimized"]) is True
    assert autostart.started_minimized(["prog"]) is False


def test_the_launch_command_survives_a_path_with_spaces():
    """"C:\\Program Files\\..." unquoted is two arguments to schtasks."""
    command = autostart.launch_command(minimized=False)

    assert command.startswith('"')
    assert sys.executable in command


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
