"""The tray icon.

The bug worth pinning: a NotifyIcon belongs to the thread that creates it, and
only a thread with a running message pump can service it. pywebview dispatches
`shown` handlers on a worker thread, so building the icon inline produced one
that never appeared in the tray - while logging "tray icon installed", because
setting Visible on a pumpless thread does not raise.
"""
from bridgebox.tray import TrayIcon, _centered_location


class FakeNative:
    """Stands in for pywebview's BrowserForm.

    InvokeRequired is the WinForms question "am I on some other thread"; Invoke
    is how you get onto the right one."""

    def __init__(self, *, invoke_required: bool = True):
        self.InvokeRequired = invoke_required
        self.invoked = 0

    def Invoke(self, func):
        self.invoked += 1
        return func()


class FakeWindow:
    def __init__(self, native=None):
        self.native = native
        self.shown = 0

    def show(self):
        self.shown += 1


def _install_with(window, monkeypatch, created: list):
    """Runs TrayIcon.install() with the .NET half replaced, so the threading
    decision can be tested on any machine."""
    tray = TrayIcon(window)

    def fake_create(*args, **kwargs):
        import threading

        created.append(threading.current_thread().name)
        tray._icon = object()
        tray.available = True
        return True

    monkeypatch.setattr(tray, "_create", fake_create)
    # The .NET imports at the top of install() are the other thing that cannot
    # run in CI; short-circuit straight to the marshalling decision.
    monkeypatch.setattr(
        TrayIcon, "install", lambda self: self._on_ui_thread(lambda: self._create())
    )
    return tray, tray.install()


def test_the_icon_is_created_through_the_forms_invoke(monkeypatch):
    """Invoke(), not a direct call: `shown` runs on a worker, and an icon built
    there is invisible even though nothing reports an error."""
    native = FakeNative(invoke_required=True)
    created: list[str] = []

    tray, ok = _install_with(FakeWindow(native), monkeypatch, created)

    assert ok is True
    assert native.invoked == 1, "the creation must be marshalled onto the UI thread"
    assert tray.available is True


def test_no_marshalling_when_already_on_the_ui_thread(monkeypatch):
    """Invoke from the UI thread onto itself is pointless work."""
    native = FakeNative(invoke_required=False)
    created: list[str] = []

    tray, ok = _install_with(FakeWindow(native), monkeypatch, created)

    assert ok is True
    assert native.invoked == 0


def test_a_window_with_no_native_form_still_installs(monkeypatch):
    """Dev and test runs have no real window; the tray must not raise there."""
    created: list[str] = []

    tray, ok = _install_with(FakeWindow(None), monkeypatch, created)

    assert ok is True


def test_an_unreachable_ui_thread_reports_unavailable(monkeypatch):
    """`available` is the answer to "is the tray a way back to the window", and
    every caller checks it - so a failed marshal must read as False, not as a
    tray that silently is not there."""

    class Exploding(FakeNative):
        def Invoke(self, func):
            raise RuntimeError("window handle is gone")

    tray = TrayIcon(FakeWindow(Exploding()))
    monkeypatch.setattr(
        TrayIcon, "install", lambda self: self._on_ui_thread(lambda: True)
    )

    assert tray.install() is False
    assert tray.available is False


# ---- when the icon exists, and what its menu says ----


class FakeMenuItem:
    def __init__(self):
        self.Enabled = True


class FakeIcon:
    def __init__(self):
        self.Text = ""
        self.Visible = True
        self.disposed = 0

    def Dispose(self):
        self.disposed += 1


def _installed_tray(**kwargs) -> TrayIcon:
    """A TrayIcon in the state install() would have left it, without .NET."""
    tray = TrayIcon(FakeWindow(None), **kwargs)
    tray._icon = FakeIcon()
    tray._stop_item = FakeMenuItem()
    tray.available = True
    return tray


def test_stopping_the_bridge_is_greyed_out_when_there_is_nothing_to_stop():
    """A menu item that is always clickable and sometimes does nothing is
    indistinguishable from one that is broken."""
    tray = _installed_tray(bridge_running=lambda: False)

    tray._sync()

    assert tray._stop_item.Enabled is False
    assert "выключен" in tray._icon.Text


def test_stopping_the_bridge_is_available_while_it_runs():
    tray = _installed_tray(bridge_running=lambda: True)

    tray._sync()

    assert tray._stop_item.Enabled is True
    assert "включён" in tray._icon.Text


def test_stopping_the_bridge_does_not_run_on_the_ui_thread():
    """Stopping closes sockets and kills a process tree - seconds of work. On
    the message loop that freezes the tray, which looks exactly like the dead
    menu item that was reported."""
    import threading

    on_ui_thread = threading.current_thread()
    ran_on: list[threading.Thread] = []
    done = threading.Event()

    def slow_stop():
        ran_on.append(threading.current_thread())
        done.set()

    tray = _installed_tray(on_stop_bridge=slow_stop, bridge_running=lambda: True)
    tray._stop_bridge()

    assert done.wait(timeout=5), "the stop handler never ran"
    assert ran_on[0] is not on_ui_thread


def test_the_icon_can_be_put_back_after_it_is_removed(monkeypatch):
    """It now comes and goes with the window - hidden puts it there, shown
    takes it away - so a one-shot install would leave a hidden app with no way
    back after the first restore."""
    created: list[str] = []
    tray, ok = _install_with(FakeWindow(FakeNative()), monkeypatch, created)
    assert ok is True

    tray.remove()
    assert tray.available is False

    assert tray.install() is True
    assert tray.available is True


def test_a_host_without_a_tray_is_not_retried_forever():
    """A missing WinForms is permanent. Retrying it on every hide would log the
    same warning for the life of the session."""
    tray = TrayIcon(FakeWindow(None))
    tray._unavailable = True

    assert tray.install() is False


def test_a_broken_bridge_check_never_greys_the_item_out():
    """Guessing "stopped" when the answer is unknowable disables the only way
    to stop a bridge that may well be running."""
    def exploding():
        raise RuntimeError("runtime is gone")

    tray = _installed_tray(bridge_running=exploding)

    tray._sync()

    assert tray._stop_item.Enabled is True


# ---- restoring from the tray -----------------------------------------------


def test_centered_location_puts_the_window_in_the_middle_of_the_screen():
    """The bug: Show() alone restores whatever Location the window already
    had, which for a window pywebview never explicitly positioned is
    wherever Windows' own cascade put it at first launch - not the centre -
    and that position then persists across every hide/show. A 1920x1080
    screen, an 960x680 window (this app's own create_window size): centred
    means 480px of margin on each side, 200px top and bottom."""
    x, y = _centered_location(0, 0, 1920, 1080, 960, 680)

    assert (x, y) == (480, 200)


def test_centered_location_accounts_for_the_working_areas_own_origin():
    """A non-primary monitor's WorkingArea does not start at (0, 0) - a
    second screen to the right of the first can have an X in the thousands.
    Ignoring area_x/area_y would centre the window on the WRONG monitor's
    coordinate space while still claiming to centre it on this one."""
    x, y = _centered_location(1920, 40, 1920, 1040, 960, 680)

    assert (x, y) == (1920 + 480, 40 + 180)
