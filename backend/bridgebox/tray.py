"""The system tray icon, so closing the window hides BridgeBox instead of
killing the bypass mid-game.

Built on System.Windows.Forms.NotifyIcon through pythonnet rather than raw
Shell_NotifyIcon, and rather than pystray. Two reasons, in order:

  - pythonnet and WinForms are ALREADY here. pywebview's Windows backend is
    WinForms; window_chrome.py already reaches into `window.native`, which is
    a BrowserForm. NotifyIcon is the platform's own tray API, so the window
    procedure, the message-loop integration and the icon lifecycle are all
    somebody else's problem - Shell_NotifyIcon would mean subclassing WndProc
    from Python and keeping a ctypes callback alive by hand.
  - pystray would be a new dependency that drags in Pillow, for one icon.

Everything is best-effort. A tray that fails to appear must leave a working
app behind, not a window nobody can get back - see `available`.
"""
from __future__ import annotations

import logging
import threading

from . import i18n

logger = logging.getLogger(__name__)

# _sync() otherwise only ran when the menu opened or a click handler asked for
# it - fine for the menu (nobody reads a closed menu's stale text), wrong for
# the tooltip, which is the ONLY status a hidden window can show and is read
# by hovering, not clicking. A machine that autostarts minimized and turns the
# bridge on via "Сразу включать мост" never opens the menu at all, so the
# tooltip built at install() - before the bridge had even tried to start -
# was what "always says выключен" actually was. Frequent enough to feel live,
# not so frequent it wakes a background process for no reason.
SYNC_INTERVAL_MS = 2000


class TrayIcon:
    """Wraps a WinForms NotifyIcon bound to the pywebview window.

    Construct it, then call `install()`. `available` says whether anything
    actually appeared; every caller must check it before relying on the tray
    to be a way back to the window."""

    def __init__(
        self,
        window,
        *,
        title: str = "BridgeBox",
        on_quit=None,
        on_stop_bridge=None,
        bridge_running=None,
        lang=None,
    ):
        self._window = window
        self._title = title
        self._on_quit = on_quit
        # Stops the bridge without closing the app, so somebody who tucked
        # BridgeBox away mid-session can turn the bypass off without first
        # digging the window back out.
        self._on_stop_bridge = on_stop_bridge
        # Read every time the menu opens, never cached: "Остановить мост" has
        # to be greyed out when there is nothing to stop, and the bridge can
        # go down on its own (the zapret watchdog) while the window is hidden.
        self._bridge_running = bridge_running
        # Same shape as bridge_running: a callable, not a value, so a language
        # switch in Settings (which promises to take effect immediately, no
        # restart) reaches the tray too. Read in _sync(), which already runs
        # on every menu open - there was no separate refresh point to add.
        self._lang = lang or (lambda: "ru")
        self._icon = None
        self._show_item = None
        self._stop_item = None
        self._quit_item = None
        self._timer = None
        self._unavailable = False
        self.available = False

    def install(self) -> bool:
        """Put the icon in the tray. Called when the window goes away, not at
        startup: an icon sitting next to a window that is already on screen is
        just clutter in a tray people keep tidy.

        Safe to call repeatedly - an icon already installed is left alone, and
        a host where the tray is impossible at all (`_unavailable`) is not
        retried, so a broken import cannot log the same warning forever."""
        if self._icon is not None or self._unavailable:
            return self.available

        try:
            import clr  # noqa: F401 - registers the .NET import hook

            clr.AddReference("System.Windows.Forms")
            clr.AddReference("System.Drawing")
            from System.Drawing import Icon, SystemIcons
            from System.Windows.Forms import (
                ContextMenuStrip,
                NotifyIcon,
                Timer,
                ToolStripMenuItem,
            )
        except Exception as exc:
            # No pythonnet, no WinForms, or a non-Windows host. The app still
            # works; it just cannot be hidden. Permanent, so it is never
            # retried - unlike a single failed install.
            self._unavailable = True
            logger.warning("tray unavailable (%s) - the window will not hide to tray", exc)
            return False

        # A NotifyIcon belongs to the thread that creates it, and only a thread
        # with a running message pump can service it. pywebview dispatches
        # `shown` handlers on a worker ("Thread-3 (execute)" in the log), so
        # building the icon inline here produced one that never appeared -
        # while still logging success, because setting Visible on a pumpless
        # thread does not raise. Marshal onto the WinForms UI thread instead.
        return self._on_ui_thread(
            lambda: self._create(
                NotifyIcon, ContextMenuStrip, ToolStripMenuItem, Icon, SystemIcons, Timer
            )
        )

    def _on_ui_thread(self, work) -> bool:
        """Run `work` on the form's own thread and return its result.

        Invoke rather than BeginInvoke: `available` is the answer to "is the
        tray a way back to the window", and every caller checks it, so it has
        to be the real outcome rather than "we asked"."""
        native = getattr(self._window, "native", None)
        try:
            if native is None or not native.InvokeRequired:
                return work()
            # clr registers the .NET import hook, and `System` is unimportable
            # without it. install() already does this, but relying on a
            # caller's side effect is how this broke under test - and would
            # break again the first time anything else called in here.
            import clr  # noqa: F401

            from System import Func, Object

            return bool(native.Invoke(Func[Object](work)))
        except Exception:
            logger.exception("could not reach the UI thread to install the tray icon")
            return False

    def _create(
        self, NotifyIcon, ContextMenuStrip, ToolStripMenuItem, Icon, SystemIcons, Timer
    ) -> bool:
        try:
            icon = NotifyIcon()
            icon.Text = self._title
            icon.Icon = self._window_icon(Icon, SystemIcons)

            menu = ContextMenuStrip()
            # Text set here is overwritten by the _sync() call at the end of
            # this method - these are just valid initial values for
            # ToolStripMenuItem's constructor.
            show_item = ToolStripMenuItem("BridgeBox")
            show_item.Click += lambda sender, args: self.show_window()
            stop_item = ToolStripMenuItem("...")
            stop_item.Click += lambda sender, args: self._stop_bridge()
            quit_item = ToolStripMenuItem("...")
            quit_item.Click += lambda sender, args: self._quit()
            menu.Items.Add(show_item)
            menu.Items.Add(stop_item)
            menu.Items.Add(quit_item)
            # Re-read on every open rather than fixed at build time: the bridge
            # can stop while this menu exists - from this very item, or because
            # winws died - and a permanently enabled "Остановить мост" is a
            # button that does nothing, which is what was reported. The
            # language can change the same way, from Settings.
            menu.Opening += lambda sender, args: self._sync()
            icon.ContextMenuStrip = menu
            self._show_item = show_item
            self._stop_item = stop_item
            self._quit_item = quit_item

            # Both, deliberately: single click is what people try first on
            # Windows, double click is what the platform documents.
            icon.Click += self._on_click
            icon.DoubleClick += lambda sender, args: self.show_window()

            icon.Visible = True
        except Exception:
            logger.exception("could not create the tray icon")
            return False

        self._icon = icon
        self.available = True
        self._sync()

        # Timer.Tick fires on the WinForms message loop, same thread the icon
        # itself lives on - no Invoke() needed the way a background thread
        # would. Keeps the tooltip honest for however long the window stays
        # hidden, not just at the moment it was hidden.
        timer = Timer()
        timer.Interval = SYNC_INTERVAL_MS
        timer.Tick += lambda sender, args: self._sync()
        timer.Start()
        self._timer = timer

        logger.info("tray icon installed")
        return True

    def _is_bridge_running(self) -> bool:
        if self._bridge_running is None:
            return True  # nothing to ask - never grey the item out on a guess
        try:
            return bool(self._bridge_running())
        except Exception:
            logger.exception("could not read the bridge state for the tray menu")
            return True

    def _sync(self) -> None:
        """Make the menu and the hover text say what is actually true.

        Never raises: this runs on the WinForms message loop, where an escaping
        exception takes the UI thread - and with it the only way back to a
        hidden window."""
        running = self._is_bridge_running()
        lang = self._lang()
        try:
            if self._show_item is not None:
                self._show_item.Text = i18n.t("tray.show", lang)
            if self._stop_item is not None:
                self._stop_item.Text = i18n.t("tray.stop_bridge", lang)
                self._stop_item.Enabled = running
            if self._quit_item is not None:
                self._quit_item.Text = i18n.t("tray.quit", lang)
            if self._icon is not None:
                # The tooltip is the only status a hidden app can show.
                key = "tray.tooltip_running" if running else "tray.tooltip_stopped"
                self._icon.Text = i18n.t(key, lang, title=self._title)
        except Exception:
            logger.exception("could not refresh the tray menu")

    def _on_click(self, sender, args):
        # A right click opens the context menu; only a left click restores.
        # Without this check the menu appears and the window pops up at once.
        try:
            from System.Windows.Forms import MouseButtons, MouseEventArgs

            if isinstance(args, MouseEventArgs) and args.Button != MouseButtons.Left:
                return
        except Exception:
            pass
        self.show_window()

    def _window_icon(self, Icon, SystemIcons):
        """The app's own icon, falling back to the generic application one.

        BridgeBox ships no .ico today, so the fallback is the normal path -
        but the window may already carry one when packaged, and reusing it
        keeps the tray and the taskbar showing the same thing."""
        try:
            native = getattr(self._window, "native", None)
            if native is not None and getattr(native, "Icon", None) is not None:
                return native.Icon
        except Exception:
            pass
        return SystemIcons.Application

    def show_window(self) -> None:
        try:
            self._window.show()
            # show() alone leaves it behind whatever the user was doing.
            native = getattr(self._window, "native", None)
            if native is not None:
                native.WindowState = _normal_window_state()
                _center_on_screen(native)
                native.Activate()
        except Exception:
            logger.exception("could not restore the window from the tray")

    def hide_window(self) -> None:
        try:
            self._window.hide()
        except Exception:
            logger.exception("could not hide the window to the tray")

    def _stop_bridge(self) -> None:
        """Turn the bypass off, leave the app running.

        On a worker thread, not here. Stopping the bridge closes sockets and
        kills a process tree - up to ten seconds - and this handler runs on the
        WinForms message loop, so doing it inline froze the whole tray for the
        duration. A frozen tray and a dead menu item look identical from
        outside, which is how "«Остановить мост» ничего не делает" was reported.

        Never raises: an exception escaping a menu handler takes the UI thread
        with it, and with it the only way back to a hidden window."""
        if self._on_stop_bridge is None:
            return

        def run() -> None:
            try:
                self._on_stop_bridge()
                logger.info("bridge stopped from the tray menu")
            except Exception:
                logger.exception("could not stop the bridge from the tray menu")
            # Back on the UI thread, so the tooltip catches up without waiting
            # for the next right click.
            self._on_ui_thread(lambda: (self._sync(), True)[1])

        threading.Thread(target=run, name="tray-stop-bridge", daemon=True).start()

    def _quit(self) -> None:
        self.remove()
        if self._on_quit is not None:
            self._on_quit()

    def remove(self) -> None:
        """Take the icon out of the tray.

        Called both on quit and every time the window comes back on screen -
        the icon exists to represent an app you cannot otherwise see, so it has
        no business sitting there next to a visible window. install() puts it
        back, which is why this resets state rather than latching anything."""
        icon, self._icon = self._icon, None
        self._show_item = None
        self._stop_item = None
        self._quit_item = None
        self.available = False
        timer, self._timer = self._timer, None
        if timer is not None:
            try:
                timer.Stop()
                timer.Dispose()
            except Exception:
                logger.exception("could not stop the tray sync timer")
        if icon is None:
            return
        try:
            icon.Visible = False
            icon.Dispose()
        except Exception:
            logger.exception("could not remove the tray icon")


def _normal_window_state():
    from System.Windows.Forms import FormWindowState

    return FormWindowState.Normal


def _center_on_screen(native) -> None:
    """Middle of whichever monitor the window was last on.

    Show() alone restores whatever Location the window already had - which,
    for a window pywebview created without an explicit position (see
    desktop.py's create_window call), is wherever Windows' own default
    cascade put it at first launch, not the centre. That position then just
    persists across every hide/show, which is what "always opens at the top
    of the screen" actually was: not a fresh top-left placement each time,
    the same one, never corrected.

    Screen.FromControl rather than Screen.PrimaryScreen so a window last
    shown on a second monitor comes back there, not wherever monitor 1 is."""
    try:
        from System.Drawing import Point
        from System.Windows.Forms import Screen

        area = Screen.FromControl(native).WorkingArea
        x, y = _centered_location(area.X, area.Y, area.Width, area.Height, native.Width, native.Height)
        native.Location = Point(x, y)
    except Exception:
        logger.exception("could not centre the window on the screen")


def _centered_location(
    area_x: int, area_y: int, area_width: int, area_height: int, window_width: int, window_height: int
) -> tuple[int, int]:
    """The plain arithmetic behind _center_on_screen, split out so it can be
    tested without a real WinForms Screen/Point - pythonnet's CLR types
    cannot be faked cheaply, but this needs nothing from them."""
    return (
        area_x + (area_width - window_width) // 2,
        area_y + (area_height - window_height) // 2,
    )
