from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

import webview

from .api.logs import LogsMixin
from .api.other_launch import OtherLaunchMixin
from .api.profiles import ProfilesMixin
from .api.steam_launch import SteamLaunchMixin
from .api.app_update import AppUpdateMixin
from .api.diagnostics import DiagnosticsMixin
from .api.system import STARTUP_INTEGRITY_DELAY_S, SystemMixin
from .api.zapret import ZapretMixin
from .config import Config, load_config, migrate_config_file, save_config
from .diagnostics import describe_exception
from . import i18n
from .log_buffer import LogBuffer
from .logging_setup import capture_std_streams, setup_logging
from .paths import PROJECT_ROOT, RESOURCE_ROOT, resolve_project_path
from .autostart import started_minimized
from .platform_support import current_windows_version, is_supported, show_unsupported_notice
from .tray import TrayIcon
from .runtime import BridgeRuntime
from .runtime_core import RuntimeCore
from .version import app_version, build_channel, display_version, release_label
from . import app_update
from . import integrity
from .zapret.strategies import resolve_zapret_layout

logger = logging.getLogger(__name__)

# RESOURCE_ROOT, not PROJECT_ROOT: this is bundled, read-only UI, not
# something a portable install writes to - see paths.py. Dev mode and the
# frozen build both resolve the same "frontend/dist/index.html" suffix, just
# rooted differently (the repo root vs. PyInstaller's extracted sys._MEIPASS).
FRONTEND_DIST = RESOURCE_ROOT / "frontend" / "dist" / "index.html"
DEV_SERVER_URL = "http://localhost:5173"

# How long the STARTUP-triggered network checks (zapret's update check, the
# app's own update check) wait before actually reaching GitHub. Windows starts
# every autostart program at once at logon, and this app's checks were
# joining that scramble the instant the window painted - fine for a check
# nobody is watching, but it is still CPU/network contention on a machine that
# is already busy loading everything else that starts with it. The manual
# "Проверить сейчас" buttons (check_zapret_update, check_app_update) do NOT
# use this - a person who clicked a button is, by definition, waiting for the
# answer now.
STARTUP_NETWORK_CHECK_DELAY_S = 4

# SMOOTH WINDOW REVEAL: pywebview's own default is #FFFFFF, painted the
# instant the native window is created - before WebView2 has initialised,
# before index.html's own boot skeleton (which covers everything AFTER that)
# has a chance to paint anything. On a dark-themed launch that is a white
# flash for however long WebView2 takes to spin up, then a second, different
# flash to the skeleton's real background. Matching this to the boot
# skeleton's own colour makes the native window and the skeleton
# indistinguishable, so there is nothing left to flash between.
#
# Values copied from frontend/index.html's --bb-boot-bg (itself copied from
# tokens.css's --color-bg - see that file's own comment on why duplication
# beats an import here) - test_desktop.py's
# test_boot_background_colors_match_the_frontend_skeleton keeps the two
# sides honest against each other the same way frontend/test/bootSkeleton.
# test.ts already does for index.html against tokens.css.
BOOT_BACKGROUND_LIGHT = "#f8fafc"
BOOT_BACKGROUND_DARK = "#0a0f1e"


def _boot_background_color(theme: str) -> str:
    return BOOT_BACKGROUND_DARK if theme == "dark" else BOOT_BACKGROUND_LIGHT


# PyInstaller's onefile bootloader hands these to the Python stage it starts,
# and they mean "the archive is already unpacked, reuse that directory instead
# of unpacking again". Passing them on to a NEW copy of the app is what breaks:
# the restarted instance skips extraction and runs out of the OLD process's
# temp directory, which that process then deletes on its way out. Whatever was
# already loaded into memory (the .pyds, the Python runtime) keeps working, so
# the window still opens - but every file read AFTER that point misses.
# The symptom this was found from: a restart (factory reset, or the wizard's
# own finish step) left the app reporting version 0.0.0 and offering an
# "update" to the version it was already running, because BOTH sources
# version.app_version() reads - the bundled pyproject.toml and the bundled
# package metadata - had been deleted out from under it.
_PYI_ONEFILE_HANDOFF_VARS = (
    "_PYI_APPLICATION_HOME_DIR",
    "_PYI_ARCHIVE_FILE",
    "_PYI_PARENT_PROCESS_LEVEL",
    # Pre-6.0 bootloaders used this name for the same handoff. Harmless to
    # clear on a build that no longer sets it.
    "_MEIPASS2",
)


def _restart_environment() -> dict[str, str]:
    """This process's environment minus PyInstaller's onefile handoff vars,
    so a relaunched copy unpacks its own archive instead of borrowing this
    one's - see _PYI_ONEFILE_HANDOFF_VARS. A source checkout has none of
    these set, so this is a plain copy of os.environ there."""
    env = os.environ.copy()
    for name in _PYI_ONEFILE_HANDOFF_VARS:
        env.pop(name, None)
    return env


def is_admin() -> bool:
    """Best-effort check for elevated (Administrator) privileges on Windows.
    Zapret/WinDivert and installing the local CA into the Trusted Root store
    both require elevation - returns False (never raises) on any platform or
    environment where the check itself isn't available, so main() fails
    closed instead of silently running half-broken."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


class Api(
    LogsMixin, ProfilesMixin, SteamLaunchMixin, OtherLaunchMixin, SystemMixin, DiagnosticsMixin,
    AppUpdateMixin, ZapretMixin,
):
    """JS-callable bridge exposed as window.pywebview.api in the frontend
    (see frontend/src/lib/bridge.ts). Every method wraps its body in
    try/except and always returns a plain dict - pywebview's various
    backends (Edge WebView2 / CEF / GTK) serialize raised Python exceptions
    into rejected JS promises differently, so a uniform {"ok": bool, "error":
    str|None, ...} shape is the only thing the frontend can rely on.

    Each domain (zapret strategies, self-update, Steam/other launch options,
    ...) lives in its own Mixin under api/ - this class just owns the shared
    state every mixin reads/writes (self._runtime, self._config, ...) plus
    whatever is small enough or cross-domain enough (bridge lifecycle,
    config, temp-dir, restart_app) not to earn its own file. pywebview still
    sees one object; frontend/src/lib/bridge.ts's callBridge calls are
    unaffected by which file a method's body lives in."""

    def __init__(
        self,
        *,
        runtime: BridgeRuntime,
        runtime_core: RuntimeCore,
        config: Config,
        config_path,
        project_root,
        log_buffer: LogBuffer,
        # Injectable so tests don't pay the real delay - see
        # STARTUP_NETWORK_CHECK_DELAY_S and start_startup_update_check.
        startup_check_delay_s: float = STARTUP_NETWORK_CHECK_DELAY_S,
        # Same reason as above - see api/system.py's own constant for why
        # the real one is as long as it is.
        integrity_delay_s: float = STARTUP_INTEGRITY_DELAY_S,
        # --tracer on the command line. Off by default: the tracer is a
        # permanent rAF loop plus an 8ms sampling timer (see
        # frontend/src/lib/motionTrace.ts), fine to carry in every build but
        # not something to run unless someone is actually diagnosing a
        # motion bug with it.
        tracer_enabled: bool = False,
    ):
        self._runtime = runtime
        self._runtime_core = runtime_core
        self._config = config
        self._config_path = config_path
        self._project_root = project_root
        self._log_buffer = log_buffer
        self._startup_check_delay_s = startup_check_delay_s
        self._integrity_delay_s = integrity_delay_s
        self._tracer_enabled = tracer_enabled
        # Strategy-suite job state, polled by test_strategies_progress().
        self._strategy_future = None
        self._strategy_results: list[dict] = []
        self._strategy_error: str | None = None
        # Which target set is currently running - "ecast"/"blobcast", or None
        # before a run starts and after it finishes. "both" runs two full
        # passes back to back (see _stages_for), so this is what lets the
        # popup say which one is in progress right now.
        self._strategy_stage: str | None = None
        # Guards the check-then-start in test_strategies() - see the comment
        # there for the double-suite race it prevents.
        self._strategy_lock = threading.Lock()
        # Serialises update_config's read-modify-write of self._config.
        self._config_lock = threading.Lock()
        # Same shape for the zapret update job.
        self._update_lock = threading.Lock()
        self._update_future = None
        self._update_state: dict = {"phase": "idle", "received": 0, "total": 0, "applied": []}
        # The automatic "Проверять при запуске" check, fired once by main()
        # via start_startup_update_check() - never by __init__ itself, since
        # constructing an Api must not have side effects a test didn't ask
        # for. None means "hasn't been started" (setting is off, or main()
        # hasn't gotten there yet); startup_update_check() is how the
        # frontend tells that apart from "still in flight" and "done".
        self._startup_update_future = None
        # Same shape, for BridgeBox's own release check - see app_update.py
        # and start_app_update_check(). A separate future/config section from
        # the zapret one above: the two check different repos, on different
        # schedules (this one defaults ON - see AppUpdateConfig), and mixing
        # their state would make "which check is this result even from" a
        # real question the frontend would have to answer.
        self._app_update_future = None
        # The self-update itself (download + swap the running .exe), kicked
        # off by start_app_apply_update() and polled via app_apply_progress -
        # same started/done-future shape as everything else here, separate
        # from _app_update_future because a check and an apply can be in
        # flight independently and the frontend needs to tell them apart.
        self._app_apply_future = None
        # Set by main() once the window exists - the folder picker needs it.
        # Stays None in dev/test, where every method must still answer with
        # the standard dict rather than raise.
        self._window = None
        # Same, for the tray icon (see attach_tray).
        self._tray = None
        # Filled by main() once the startup check has run; None means it
        # has not, which reads as "nothing to report" rather than a scare.
        self._integrity = None
        # Guards start_integrity_check/notify_ui_settled against each other -
        # two entry points now race to start the same background hash, and
        # only the first should win. See api/system.py's _start_integrity_check.
        self._integrity_started = False
        self._integrity_lock = threading.Lock()
        # Guards apply_steam_launch_options/revert_steam_launch_options
        # against a second call while one is still closing/rewriting/
        # reopening Steam - the "applying" modal's native <dialog> can be
        # Escape-dismissed back to idle mid-operation, and two concurrent
        # runs against the same file and backup store would race. A bare
        # bool (no lock) is enough: the realistic trigger is a human
        # double-click/Escape-then-reclick, not a tight race, and the whole
        # operation this guards is itself many seconds long - see
        # _update_lock above for the fuller-lock version used elsewhere,
        # kept simple here on purpose.
        self._steam_launch_busy: bool = False
        # Steam's scan is synchronous (a few small VDF reads) - the "Прочие
        # копии" scan walks every local drive, which can take minutes, so it
        # needs the same submit-and-poll job shape as test_strategies rather
        # than a plain blocking call.
        self._other_scan_future = None
        self._other_scan_lock = threading.Lock()
        self._other_scan_progress: dict = {"foldersChecked": 0}
        # Guards apply_other_launch_options/revert_other_launch_options the
        # same way _steam_launch_busy guards the Steam equivalents.
        self._other_launch_busy: bool = False

    def attach_window(self, window) -> None:
        """Hand the pywebview window to Api after create_window().

        Explicit rather than reaching for webview.windows[0]: that couples
        this class to module-global state and makes it untestable without a
        real webview."""
        self._window = window

    def ping(self) -> str:
        return "pong"

    def _layout(self):
        """Resolve zapret's on-disk layout from the *current* config - the
        strategy dir can move between calls if the user edits zapret.dir."""
        return resolve_zapret_layout(
            resolve_project_path(self._project_root, self._config.zapret.dir)
        )

    # ---- bridge lifecycle ----

    def bridge_start(self) -> dict:
        try:
            status = self._runtime.start()
            return {"ok": True, "error": None, **status}
        except Exception as exc:
            return {"ok": False, "error": str(exc), **self._runtime.get_status()}

    def bridge_stop(self) -> dict:
        try:
            status = self._runtime.stop()
            return {"ok": True, "error": None, **status}
        except Exception as exc:
            return {"ok": False, "error": str(exc), **self._runtime.get_status()}

    def bridge_status(self) -> dict:
        try:
            return {"ok": True, "error": None, **self._runtime.get_status()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def tracer_enabled(self) -> bool:
        """Whether the app was launched with --tracer.

        main.tsx asks this before installing the motion tracer - see its own
        docstring for what the tracer is and why it stays out of the way
        unless someone specifically asked for it."""
        return self._tracer_enabled

    # ---- config ----

    def get_config(self) -> dict:
        try:
            return {"ok": True, "error": None, "config": self._config.model_dump()}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "config": None}

    def update_config(self, patch: dict) -> dict:
        try:
            # The whole read-modify-write under one lock. pywebview dispatches
            # every JS call on its own thread (the logs show four config saves
            # inside 26ms across four threads), so two patches arriving
            # together both used to read the same starting config and the
            # second one to finish would write its copy over the first's -
            # a silently lost setting, with the write itself reporting ok.
            with self._config_lock:
                merged = self._config.model_dump()
                _deep_merge(merged, patch)
                new_config = Config.model_validate(merged)
                save_config(new_config, self._config_path)
                self._config = new_config
                self._runtime_core.set_config(new_config)
            # The native title bar is painted by Windows and knows nothing
            # about tokens.css, so it has to be repainted whenever the theme
            # could have moved. Hooked here rather than behind its own Api
            # method because every route that changes the theme already comes
            # through update_config - the Settings toggle, and the factory
            # reset that puts it back to dark.
            self.apply_window_theme()
            return {"ok": True, "error": None, "config": new_config.model_dump()}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "config": self._config.model_dump()}

    # ---- autostart / tray ----

    def attach_tray(self, tray) -> None:
        """Same reasoning as attach_window: built by main() once the window
        exists, handed over rather than reached for through a global."""
        self._tray = tray

    def wants_tray_on_close(self) -> bool:
        """Read at close time, not captured at startup, so the Settings toggle
        takes effect on the very next close rather than after a restart."""
        return self._config.ui.minimize_to_tray

    def current_language(self) -> str:
        """"ru" or "en", resolved from the "system"/"ru"/"en" preference -
        for the tray and any other backend-owned text. Read fresh every time,
        same as wants_tray_on_close above, so a language switch in Settings
        reaches the tray without a restart."""
        return i18n.resolve_locale(self._config.ui.language)

    def open_external_url(self, url: str) -> dict:
        """Hand `url` to the OS's default browser via webbrowser.open(),
        rather than a plain `<a target="_blank">` - WebView2's handling of a
        new-window navigation is not guaranteed to leave the app's own window
        at all, and the bug-report links (GitHub Issues, Google Forms) need
        to reliably land in the user's real browser. http(s) only: this takes
        a URL straight from a button's onClick, not user input, but a wrong
        scheme (`file:`, `javascript:`) is cheap to reject outright."""
        if urlsplit(url).scheme not in ("http", "https"):
            return {"ok": False, "error": "unsupported url scheme"}
        try:
            webbrowser.open(url)
            return {"ok": True, "error": None}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def app_info(self) -> dict:
        """The app's version and the short label the Beta badge shows.

        Both derived from pyproject.toml (see version.py) rather than a second
        copy in the frontend - `label` is empty on a final release, which is
        how the badge disappears without a code change."""
        try:
            version = app_version()
            return {
                "ok": True,
                "error": None,
                # What the user is shown ("0.1"), not the PEP 440 string
                # packaging tools need ("0.1.0b1").
                "version": display_version(version),
                # Non-empty only while this is a pre-release; the badge is
                # rendered on that, and shows β rather than this text.
                "label": release_label(version),
                "channel": build_channel(),
            }
        except Exception as exc:
            return {
                "ok": False, "error": describe_exception(exc),
                "version": "", "label": "", "channel": "",
            }

    # ---- zapret strategies ----

    # ---- temp directory ----

    def _temp_root(self) -> Path:
        """Where downloads and extraction go. An empty setting means the
        system temp dir, so the feature works with no configuration."""
        configured = self._config.paths.temp_dir.strip()
        if not configured:
            return Path(tempfile.gettempdir()) / "bridgebox"
        return resolve_project_path(self._project_root, configured)

    def get_temp_dir(self) -> dict:
        try:
            return {
                "ok": True,
                "error": None,
                "path": self._config.paths.temp_dir,
                # What it actually resolves to - "temp" alone tells the user
                # nothing about where their disk is being used.
                "resolved": str(self._temp_root()),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "path": "", "resolved": ""}

    def pick_temp_dir(self) -> dict:
        """Open the native folder picker and save the choice."""
        try:
            if self._window is None:
                return {"ok": False, "error": "окно недоступно", "path": ""}
            chosen = self._window.create_file_dialog(webview.FOLDER_DIALOG)
            if not chosen:
                return {"ok": True, "error": None, "path": self._config.paths.temp_dir}
            path = chosen[0] if isinstance(chosen, (list, tuple)) else str(chosen)
            result = self.update_config({"paths": {"temp_dir": str(path)}})
            return {"ok": result["ok"], "error": result["error"], "path": str(path)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "path": ""}

    def restart_app(self) -> dict:
        """Relaunch BridgeBox and close this window.

        sys.executable rather than run.bat: the batch file re-runs the
        elevation check, an mtime scan and potentially a full Vite build, and
        opens a console - all to restart the same interpreter for an update
        that touched no frontend file. The child inherits this process's
        elevated token, so there is no second UAC prompt."""
        try:
            if getattr(sys, "frozen", False):
                command = [sys.executable]
            else:
                command = [sys.executable, "-m", "bridgebox.desktop"]

            self._runtime.stop()
            subprocess.Popen(
                command,
                cwd=str(self._project_root),
                env=_restart_environment(),
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            logger.info("restarting: %s", " ".join(command))
            if self._window is not None:
                self._window.destroy()
            return {"ok": True, "error": None}
        except Exception as exc:
            logger.exception("restart failed")
            return {"ok": False, "error": describe_exception(exc)}


def reconcile_self_update(exe_path: Path | None, project_root: Path) -> None:
    """Clean up whatever a self-update's relaunch script left behind, and
    record a fresh integrity baseline if it actually swapped
    bridgebox.exe/_internal/ before this process started.

    The `.old` backup a completed swap leaves behind (see
    app_update.build_relaunch_script), or the `.new` stage of one that
    downloaded but never got applied (verify_exe_digest refused it, or the
    app closed before "Перезапустить сейчас"), can only be cleaned up now:
    the relaunch script's own handle on `.old` is long gone by the time THIS
    process exists to see it. A `.old` backup having existed means
    integrity.py's baseline still describes the OLD files - without
    re-recording it here, the very first launch after a self-update would
    show "files were modified" over the update it just installed, not over
    tampering. `exe_path` is None in dev mode (running_exe_path() has no
    installed exe to report), where this is a no-op; best-effort otherwise,
    since a failure here should never stop the app from starting."""
    if exe_path is None:
        return
    try:
        if app_update.cleanup_stale_files(exe_path):
            integrity.write_manifest(project_root)
    except Exception:
        logger.exception("could not clean up leftover self-update files - continuing")


def _deep_merge(base: dict, patch: dict) -> None:
    """Merge a settings patch into a config dump, in place.

    A null value *removes* the key instead of setting it to None, so pydantic
    fills the field's default back in on the next model_validate. That is
    what makes update_config({"rewrite": None}) a working "reset this section"
    - no field is nullable, so null could never have meant anything else."""
    for key, value in patch.items():
        if value is None:
            base.pop(key, None)
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def main(
    *,
    admin_check=is_admin,
    windows_version=current_windows_version,
    startup_check_delay_s: float = STARTUP_NETWORK_CHECK_DELAY_S,
) -> None:
    # Before the admin check on purpose: there is no reason to make someone
    # clear a UAC prompt only to be told the app cannot run here at all.
    version = windows_version()
    if not is_supported(version):
        logger.error("unsupported Windows: %s", version.describe() if version else "unknown")
        show_unsupported_notice(version)
        raise SystemExit(
            "BridgeBox requires Windows 10 or newer - WebView2, which draws "
            "the entire UI, is no longer supported on Windows 7/8/8.1."
        )

    if not admin_check():
        # Before setup_logging/capture_std_streams below, so sys.stderr is
        # still whatever the OS gave this process - None under the frozen
        # build's windowed subsystem (no console at all) or under pythonw.
        # A bare print() here raised AttributeError instead of explaining
        # anything, in exactly the launch mode this message exists for.
        if sys.stderr is not None:
            print(
                "BridgeBox must run as Administrator - it's required for Zapret/"
                "WinDivert and for installing the local TLS certificate. "
                "Run run.bat instead of launching this module directly (it "
                "requests elevation via UAC), or start this from an already "
                "elevated terminal.",
                file=sys.stderr,
            )
        raise SystemExit(1)

    config_path = PROJECT_ROOT / "config.yaml"
    config = load_config(config_path)
    # SECURITY/UX FIX (safe config migration): add any default field a newer
    # BridgeBox introduced to an EXISTING config.yaml, without touching a
    # single value the user already set - see migrate_config_file's own
    # docstring. Before setup_logging on purpose: logging.dir itself could be
    # one of the fields a future version adds, and this has to see the file
    # as it will be read from now on.
    try:
        migrate_config_file(config_path)
    except Exception:
        logger.exception("could not migrate config.yaml - continuing with what loaded")

    reconcile_self_update(app_update.running_exe_path(), PROJECT_ROOT)

    log_buffer = LogBuffer()
    logging_config = config.logging.model_copy(
        update={"dir": str(resolve_project_path(PROJECT_ROOT, config.logging.dir))}
    )
    setup_logging(logging_config, ui_sink=log_buffer.append)
    # After setup_logging, never before: the console handler is built against
    # the ORIGINAL stderr, and this replaces the current one. Under pythonw
    # (how run.bat launches BridgeBox) both streams are None, so a stray print
    # or a traceback from a library was not merely invisible - it raised.
    capture_std_streams()

    runtime_core = RuntimeCore(config=config, project_root=PROJECT_ROOT)
    runtime = BridgeRuntime(runtime_core)

    api = Api(
        runtime=runtime,
        runtime_core=runtime_core,
        config=config,
        config_path=config_path,
        project_root=PROJECT_ROOT,
        log_buffer=log_buffer,
        startup_check_delay_s=startup_check_delay_s,
        tracer_enabled="--tracer" in sys.argv,
    )
    dev_mode = "--dev" in sys.argv
    if dev_mode:
        # Development only, and it matters why: this loads the UI over plain
        # HTTP from a local port, and everything served there gets the full
        # window.pywebview.api surface - restart_app (which spawns a process),
        # update_config (which writes config.yaml), the file dialogs - in a
        # process running as Administrator. Any local program that manages to
        # own port 5173 first inherits all of it. Never ship a build that
        # reaches this branch by default.
        logger.warning(
            "dev mode: loading the UI from %s over plain HTTP - the pywebview "
            "api is exposed to whatever answers on that port",
            DEV_SERVER_URL,
        )
        url = DEV_SERVER_URL
    elif FRONTEND_DIST.exists():
        url = str(FRONTEND_DIST)
    else:
        raise SystemExit(
            f"Frontend build not found at {FRONTEND_DIST}. Run `npm run build` in "
            "frontend/, or launch with --dev while `npm run dev` is running."
        )

    # The minimized autostart task launches with --minimized; the window is
    # created hidden so nothing flashes on the desktop at logon.
    #
    # BUG FIX: this used to read config.ui.minimize_to_tray - a DIFFERENT
    # setting ("hide to tray when the window's X is clicked") that has
    # nothing to do with how the app was launched. A machine with "Запускать
    # свернутым в трей" (autostart_minimized) on but "Сворачивать в трей при
    # закрытии" (minimize_to_tray) off still opened a visible window on every
    # logon - the setting the user actually turned on was never consulted.
    # --minimized only ever appears in argv via the task this same setting
    # writes (see autostart._launch_command_parts), so checking it alone is
    # enough; no second, independently-driftable config read is needed here.
    hidden = started_minimized()
    window = webview.create_window(
        "BridgeBox",
        url,
        js_api=api,
        width=960,
        height=680,
        min_size=(760, 560),
        hidden=hidden,
        background_color=_boot_background_color(config.ui.theme),
    )
    # The folder picker and the restart both need the window; Api is built
    # before it exists, so it is handed over here rather than reached for
    # through webview.windows[0].
    api.attach_window(window)
    # Not straight after create_window(): the HWND the title bar is painted
    # through does not exist until the window is actually shown, so applying
    # it any earlier is a silent no-op.
    window.events.shown += api.apply_window_theme

    # Guards the whole teardown. Set the moment a close is accepted, so a
    # second click on the X - which is exactly what an app that appears frozen
    # invites - cannot start a second shutdown over the first.
    closing = threading.Event()

    tray = TrayIcon(
        window,
        on_quit=lambda: _begin_shutdown(window, runtime, tray, closing),
        # Stops the bypass without closing the app - the point of hiding to the
        # tray is that the session survives, so turning it off has to be
        # reachable from there too.
        on_stop_bridge=runtime.stop,
        # Greys "Остановить мост" out when there is nothing to stop.
        bridge_running=lambda: bool(runtime.get_status().get("running")),
        lang=api.current_language,
    )
    api.attach_tray(tray)

    window.events.closing += lambda: _on_closing(window, runtime, tray, api, closing)

    def on_shown() -> None:
        """Everything deferred until the window is actually on screen.

        Nothing here blocks: the tray removal is local, and both of the others
        hand work to the background loop and return. But nothing here starts
        BEFORE the first paint either - the update check in particular reaches
        GitHub, on the networks this app exists for, and starting that while
        the UI was still coming up made a cold launch feel slow for a result
        nobody is waiting on.

        `shown` fires again every time the window returns from the tray, so all
        four must be safe to call repeatedly - remove() no-ops without an
        icon, start_bridge_on_launch() no-ops once the bridge is up, and both
        update checks refuse to start a second run while one is in flight."""
        tray.remove()
        api.start_bridge_on_launch()
        api.start_startup_update_check()
        api.start_app_update_check()
        api.start_integrity_check()

    window.events.shown += on_shown

    if hidden:
        # Started by the minimized autostart task: `shown` will not fire, so
        # without this the app would be running with no window AND no icon -
        # unreachable. `loaded` is the earliest point the native form exists.
        window.events.loaded += lambda: tray.install()

    webview.start()


def _on_closing(window, runtime, tray, api, closing: threading.Event) -> bool:
    """Decide what happens to a FormClosing event. True lets it through,
    False cancels it - that is pywebview's contract, not this app's.

    Three outcomes, in order:

    1. A teardown is already running - this is Close() firing FormClosing a
       SECOND time. destroy_window() calls Close() once the background
       teardown is genuinely finished, and that trip through the same event
       is indistinguishable from a second click at this point. Returning
       False here (the old bug) cancelled that Close() too, on every close,
       forever - the app never actually exited, just re-showed a closing
       overlay that had already run to completion. Let it through.
    2. "Сворачивать в трей" is on and the icon actually appeared - hide.
       install() is what decides that, and it is called HERE rather than at
       startup: the tray icon represents an app you cannot see, so it has no
       business existing while the window is on screen. A tray that could not
       be created falls through to a real close rather than swallowing it,
       which would leave a window nobody can get rid of.
    3. Otherwise: show the closing overlay and tear down in the background,
       cancelling this first Close() so the window stays up while it does.

    This handler runs on the GUI thread - so doing the teardown inline is
    exactly what made closing look like a freeze. The teardown thread calls
    destroy() when it is genuinely finished, which is outcome 1 above."""
    if closing.is_set():
        return True
    if api.wants_tray_on_close() and tray.install():
        tray.hide_window()
        return False
    _begin_shutdown(window, runtime, tray, closing)
    return False


# The overlay the UI paints while the app is shutting down. Dispatched as a
# DOM event rather than called as a function so the frontend owns what it looks
# like, and so a frontend that never registered a listener - or crashed - costs
# nothing but a missing animation.
_CLOSING_EVENT_JS = "window.dispatchEvent(new Event('bb:closing'))"


def _begin_shutdown(window, runtime, tray, closing: threading.Event) -> None:
    """Block the UI, tear everything down, then close for real.

    Stopping the bridge means closing sockets and killing a process tree, which
    takes seconds. Doing it on the GUI thread - which is what the close handler
    used to do - left a window that painted nothing and answered nothing, and
    looked exactly like a hang. So: tell the UI first, work on a thread, and
    destroy the window only once there is genuinely nothing left running.

    The tray "Выход" item comes through here too, for the same reason and to
    get the same overlay."""
    if closing.is_set():
        return
    closing.set()

    def teardown() -> None:
        try:
            # NOT from the caller's thread: on_closing() runs this function on
            # the GUI thread itself, and edgechromium's evaluate_js() blocks
            # that same thread on a semaphore that only its own WebView2
            # continuation - scheduled through the GUI thread's sync context -
            # can release. Called from there, it never returns: the window
            # goes "Not Responding" before the overlay even shows, and nothing
            # after it (runtime.shutdown(), destroy()) ever runs. Off the GUI
            # thread, Invoke() marshals for real and the continuation has a
            # free message loop to run on.
            window.evaluate_js(_CLOSING_EVENT_JS)
        except Exception:
            # A UI that cannot be told is not a reason to refuse to shut down.
            logger.debug("could not show the closing overlay", exc_info=True)
        try:
            # Idempotent, and it is what actually waits for zapret and the
            # listeners to be gone.
            runtime.shutdown()
        except Exception:
            logger.exception("shutdown did not finish cleanly")
        try:
            tray.remove()
        except Exception:
            logger.exception("could not remove the tray icon while closing")
        logger.info("shutdown complete - closing the window")
        try:
            # Safe from this thread: pywebview marshals destroy onto the GUI
            # thread itself (winforms destroy_window uses Invoke).
            window.destroy()
        except Exception:
            logger.exception("could not close the window")

    threading.Thread(target=teardown, name="bridgebox-shutdown", daemon=True).start()


if __name__ == "__main__":
    main()
