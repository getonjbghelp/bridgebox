"""The run.bat entry point.

Its whole job is that a failure before logging exists still leaves a message
somewhere. Under pythonw.exe sys.stderr is None, so the old failure mode was
not "hard to find" - it was a window that never appeared and not one byte
written anywhere.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

# Loaded by path, not imported: it is deliberately NOT part of the bridgebox
# package, because a venv too broken to import bridgebox is exactly the case it
# has to be able to report.
_LAUNCHER_PATH = Path(__file__).resolve().parents[2] / "launcher.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("bridgebox_launcher", _LAUNCHER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def launcher():
    return _load_launcher()


def test_the_launcher_lives_outside_the_package(launcher):
    """If it were `-m bridgebox.launcher`, a venv that cannot import bridgebox
    could not report that through it - which is the failure it exists for."""
    assert _LAUNCHER_PATH.exists()
    assert _LAUNCHER_PATH.parent.name != "bridgebox"


def test_a_missing_stderr_is_replaced_with_a_file(launcher, tmp_path, monkeypatch):
    """pythonw gives the process no stderr at all, and every write to it
    raises. This is the whole reason the file exists."""
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(launcher, "__file__", str(tmp_path / "launcher.py"))

    sink = launcher._stderr_sink()

    try:
        assert sink is not None
        assert sys.stderr is sink
        sink.write("boom\n")
        sink.flush()
        written = (tmp_path / "logs" / launcher.LOG_NAME).read_text(encoding="utf-8")
    finally:
        sink.close()

    assert "boom" in written


def test_a_real_console_is_left_alone(launcher, tmp_path, monkeypatch):
    """Under `run.bat --console` stderr is a console the user is looking at.
    Redirecting it to a file there would empty the very window they asked for."""

    class FakeConsole:
        pass

    console = FakeConsole()
    monkeypatch.setattr(sys, "stderr", console)
    monkeypatch.setattr(launcher, "__file__", str(tmp_path / "launcher.py"))

    assert launcher._stderr_sink() is console
    assert not (tmp_path / "logs").exists(), "no file should have been created"


def test_an_unwritable_log_does_not_stop_the_app(launcher, tmp_path, monkeypatch):
    """The log is a diagnostic. Refusing to start because it could not be
    opened would trade a missing message for a missing application."""
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(launcher, "__file__", str(tmp_path / "launcher.py"))

    def refuse(*args, **kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(launcher.Path, "mkdir", refuse)

    assert launcher._stderr_sink() is None


def test_the_log_is_truncated_rather_than_appended(launcher, tmp_path, monkeypatch):
    """It holds the LAST crash, not a year of them. Everything after logging is
    configured goes to bridgebox.log, which rotates; this one does not."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / launcher.LOG_NAME).write_text("from a previous launch\n", encoding="utf-8")

    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(launcher, "__file__", str(tmp_path / "launcher.py"))

    sink = launcher._stderr_sink()
    try:
        sink.write("today\n")
        sink.flush()
        written = (log_dir / launcher.LOG_NAME).read_text(encoding="utf-8")
    finally:
        sink.close()

    assert "from a previous launch" not in written
    assert "today" in written


def test_a_crash_is_written_to_the_raw_file_not_through_the_logger(
    launcher, tmp_path, monkeypatch
):
    """By the time main() raises, capture_std_streams may have replaced
    sys.stderr with the logger - whose handlers can already be closing down. A
    crash report that depends on the thing that crashed is no report at all."""
    monkeypatch.setattr(sys, "stderr", None)
    monkeypatch.setattr(launcher, "__file__", str(tmp_path / "launcher.py"))

    def explode():
        # Stand in for the real import, and move sys.stderr out from under the
        # handler the way capture_std_streams() does.
        sys.stderr = object()
        raise RuntimeError("the venv is stale")

    monkeypatch.setattr(
        launcher, "_stderr_sink", lambda: _open_and_track(launcher, tmp_path, opened)
    )
    opened: list = []
    monkeypatch.setitem(sys.modules, "bridgebox.desktop", _FakeDesktop(explode))

    with pytest.raises(RuntimeError):
        launcher.main()

    opened[0].close()
    written = (tmp_path / "logs" / launcher.LOG_NAME).read_text(encoding="utf-8")
    assert "the venv is stale" in written


def _open_and_track(launcher, tmp_path, opened):
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handle = open(log_dir / launcher.LOG_NAME, "w", encoding="utf-8", buffering=1)
    opened.append(handle)
    return handle


class _FakeDesktop:
    """Stands in for bridgebox.desktop so `from ... import main` finds it."""

    def __init__(self, main):
        self.main = main
