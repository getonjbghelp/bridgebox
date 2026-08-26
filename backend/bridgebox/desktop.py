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
import uuid
import webbrowser
from concurrent.futures import CancelledError
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
import webview

from .config import Config, load_config, migrate_config_file, rewrite_for, save_config
from .diagnostics import (
    BLOBCAST_TARGETS,
    ECAST_TARGETS,
    build_switch,
    describe_exception,
    probe_targets,
    render_strategy_results_html,
    render_strategy_results_json,
    run_strategy_suite,
)
from . import i18n
from . import integrity
from .log_buffer import EXPORT_FORMATS, LogBuffer, render_log
from .logging_setup import capture_std_streams, setup_logging
from .paths import PROJECT_ROOT, RESOURCE_ROOT, resolve_project_path
from .autostart import disable as disable_autostart
from .autostart import enable as enable_autostart
from .autostart import is_enabled as autostart_is_enabled
from .autostart import started_minimized
from .platform_support import current_windows_version, is_supported, show_unsupported_notice
from .tray import TrayIcon
from .profiles_io import export_payload, import_payload
from .runtime import BridgeRuntime
from .runtime_core import RuntimeCore
from .server.rooms import redact, rewrite_server_field
from . import other_launch
from . import steam_launch
from .tls.ca import CA_CERT_FILENAME
from .version import app_version, build_channel, display_version, release_label
from . import app_update
from .window_chrome import THEMED_NONE, apply_titlebar_theme
from .zapret.strategies import (
    discover_strategies,
    group_strategies,
    resolve_strategy,
    resolve_zapret_layout,
)
from .zapret.strategies import save_hostlist as write_hostlist
from .zapret.process import console_flags, kill_all_winws, stop_windivert_service, wait_for_winws_exit
from .zapret import update as zapret_update

logger = logging.getLogger(__name__)

# RESOURCE_ROOT, not PROJECT_ROOT: this is bundled, read-only UI, not
# something a portable install writes to - see paths.py. Dev mode and the
# frozen build both resolve the same "frontend/dist/index.html" suffix, just
# rooted differently (the repo root vs. PyInstaller's extracted sys._MEIPASS).
FRONTEND_DIST = RESOURCE_ROOT / "frontend" / "dist" / "index.html"
DEV_SERVER_URL = "http://localhost:5173"

# A real, currently-active apptag confirmed against live traffic (see the
# room-creation shape rewrite_server_field/rooms.py handle) - used purely to
# exercise the room-create + room-lookup round trip; test_connection stops
# there and does not attempt a WS relay connect (see _test_connection_coro).
TEST_APPTAG = "fourbage"

# steam_launch.quit_steam's own worst case: a `-shutdown` subprocess call, a
# poll loop that runs up to _GRACEFUL_QUIT_TIMEOUT_S, and a forced `taskkill`
# call - each of those subprocess calls has its own 10s timeout - before the
# file is ever touched. Derived from the module's own constant (times 3 for
# the three subprocess timeouts, plus real margin) rather than a bare magic
# number, so the two can't silently drift apart again: a `timeout=30` here
# used to let the Api layer report failure while quit_steam kept running in
# the background and the file still got rewritten underneath the user.
STEAM_LAUNCH_API_TIMEOUT_S = steam_launch._GRACEFUL_QUIT_TIMEOUT_S * 3 + 60

# No process to close first (unlike Steam) - patching a handful of files/
# shortcuts via COM is a matter of seconds even on a slow disk.
OTHER_LAUNCH_API_TIMEOUT_S = 60

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


def _redacted_json(value) -> str:
    """Render a decoded API payload for a UI step, credentials blanked.

    The room-creation response carries "token" - the credential that controls
    the room - and these strings are shown in the диагностика popup and
    routinely pasted into bug reports. Interpolating the parsed body raw put
    that token on screen, bypassing the SENSITIVE_BODY_KEYS discipline that
    already covers the log for exactly this payload."""
    return redact(json.dumps(value, ensure_ascii=False, default=str))


def _find_key(node, key: str) -> str | None:
    """First string stored under `key` at any depth. The creation response
    wraps its payload ({"ok":true,"body":{...}}), so a flat top-level lookup
    misses - the same reason rewrite_server_field walks the whole document
    instead of reading fixed keys."""
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key and isinstance(value, str):
                return value
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None


class Api:
    """JS-callable bridge exposed as window.pywebview.api in the frontend
    (see frontend/src/lib/bridge.ts). Every method wraps its body in
    try/except and always returns a plain dict - pywebview's various
    backends (Edge WebView2 / CEF / GTK) serialize raised Python exceptions
    into rejected JS promises differently, so a uniform {"ok": bool, "error":
    str|None, ...} shape is the only thing the frontend can rely on."""

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
    ):
        self._runtime = runtime
        self._runtime_core = runtime_core
        self._config = config
        self._config_path = config_path
        self._project_root = project_root
        self._log_buffer = log_buffer
        self._startup_check_delay_s = startup_check_delay_s
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

    def get_autostart(self) -> dict:
        """What Windows actually has, not what config.yaml believes.

        The two can disagree - somebody can delete the task in Task Scheduler,
        or a restore can bring it back - and the truth is the task, so the
        toggle reflects that rather than a stale flag."""
        try:
            return {
                "ok": True,
                "error": None,
                "enabled": autostart_is_enabled(),
                "minimized": self._config.ui.autostart_minimized,
            }
        except Exception as exc:
            return {"ok": False, "error": describe_exception(exc), "enabled": False,
                    "minimized": False}

    def set_autostart(self, enabled: bool, minimized: bool = False) -> dict:
        """Create or remove the logon task, then record what was asked for.

        The config write happens either way: it carries `minimized`, which the
        task itself does not report back, and it keeps the UI honest about the
        user's intent even when schtasks refused."""
        try:
            ok = enable_autostart(minimized=minimized) if enabled else disable_autostart()
            self.update_config(
                {"ui": {"autostart": bool(enabled and ok), "autostart_minimized": minimized}}
            )
            return {
                "ok": ok,
                "error": None if ok else "Windows не дал создать задачу автозапуска",
                "enabled": autostart_is_enabled(),
                "minimized": minimized,
            }
        except Exception as exc:
            logger.exception("failed to change autostart")
            return {"ok": False, "error": describe_exception(exc), "enabled": False,
                    "minimized": minimized}

    def start_bridge_on_launch(self) -> None:
        """Fire the bridge in the background at startup, if that is switched
        on. Never blocks the window: binding a port and spawning winws takes
        seconds, and a first paint held hostage to it looks like a hang."""
        if not self._config.ui.start_bridge_on_launch:
            return
        # Called from `shown`, which fires again on every restore from the
        # tray - so this must not try to start a bridge that is already up.
        try:
            if self._runtime.get_status().get("running"):
                return
        except Exception:
            logger.exception("could not read bridge status before the automatic start")
            return
        logger.info("starting the bridge automatically (start_bridge_on_launch)")

        def run() -> None:
            result = self.bridge_start()
            if not result.get("ok"):
                logger.error("automatic bridge start failed: %s", result.get("error"))

        # A plain thread rather than runtime.submit: bridge_start is the
        # SYNCHRONOUS Api method, and it already blocks on the background loop
        # for up to 20s internally. Handing it to submit() would need a
        # coroutine, and calling it here would freeze the first paint.
        threading.Thread(target=run, name="bridge-autostart", daemon=True).start()

    # ---- integrity of our own files ----

    def integrity_status(self) -> dict:
        """Whether BridgeBox's files still match the baseline.

        Read from the cached result rather than re-hashing: this is polled by
        every screen that shows the banner, and walking the tree per call would
        turn a warning into a disk load. The check runs once at startup and
        again after an update, which are the only two moments it can change
        without the app knowing."""
        try:
            report = self._integrity or integrity.IntegrityReport(verified=True)
            return {
                "ok": True,
                "error": None,
                "dismissed": self._config.ui.integrity_warning_dismissed,
                **report.as_dict(),
            }
        except Exception as exc:
            return {"ok": False, "error": describe_exception(exc), "verified": True,
                    "dismissed": True, "changed": [], "missing": [], "added": [],
                    "total": 0, "baselineMissing": False}

    def dismiss_integrity_warning(self) -> dict:
        """Never show the banner again. Reversible from the config file, which
        is where somebody who changes their mind will already be looking."""
        try:
            return self.update_config({"ui": {"integrity_warning_dismissed": True}})
        except Exception as exc:
            return {"ok": False, "error": describe_exception(exc), "config": None}

    def start_integrity_check(self) -> None:
        """Hash our own files once, in the background.

        A thread, not the event loop: this is blocking disk I/O over a few
        hundred files, and the loop serves every other Api call. Nothing waits
        for it - the banner simply appears a moment after the window does,
        which is the right order for a warning nobody can act on instantly."""
        if self._integrity is not None:
            return  # `shown` fires again on every restore from the tray

        def run() -> None:
            self._integrity = integrity.ensure_baseline(self._project_root)

        threading.Thread(target=run, name="integrity-check", daemon=True).start()

    def install_certificate(self) -> dict:
        """Issue the certificates and trust the CA, without starting anything.

        The first-run wizard's mandatory step. Until it existed the only route
        to a trusted CA was bridge_start(), which also binds ports and launches
        winws - far too much to happen behind a button that says it installs a
        certificate.

        Blocking rather than a polled background job like the strategy suite:
        this is one certutil call plus RSA keygen, seconds rather than minutes.
        """
        try:
            _, installed = self._runtime_core.ensure_certificate()
            return {
                "ok": bool(installed),
                "error": None if installed else "не удалось добавить сертификат в доверенные корневые",
                "certInstalled": bool(installed),
            }
        except Exception as exc:
            logger.exception("certificate install failed")
            return {"ok": False, "error": describe_exception(exc), "certInstalled": False}

    def apply_window_theme(self) -> bool:
        """Repaint the native title bar for the current config's theme.

        Safe before the window exists (dev, tests): window_chrome reports no
        handle and does nothing. Called on every config write and once from
        main() when the window is first shown.

        Collapses window_chrome's three-valued result to "did anything
        happen", because that is all a caller here can act on - Windows 10
        getting only the light/dark flag is a success, not a failure."""
        if self._window is None:
            return False
        return apply_titlebar_theme(self._window, self._config.ui.theme) != THEMED_NONE

    # ---- zapret strategies ----

    def list_strategies(self) -> dict:
        try:
            strategies = discover_strategies(self._layout().strategies_dir)
            grouped = group_strategies(strategies)
            return {
                "ok": True,
                "error": None,
                "groups": {
                    group: [{"key": s.key, "name": s.filename.removesuffix(".bat")} for s in items]
                    for group, items in grouped.items()
                },
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "groups": {}}

    # ---- zapret hostlist ----

    def get_hostlist(self) -> dict:
        """The domains winws.exe applies its DPI bypass to. Editable from
        Settings because the set of Jackbox hosts is not ours to freeze - a
        new shard hostname turning up in the logs is exactly the case where
        the user needs to add one without shipping a build."""
        try:
            path = self._layout().hostlist_path
            text = path.read_text(encoding="utf-8") if path.exists() else ""
            return {"ok": True, "error": None, "text": text}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "text": ""}

    def save_hostlist(self, text: str) -> dict:
        try:
            hosts = write_hostlist(self._layout().hostlist_path, text)
            # zapret/lists/*.txt is one of integrity.py's WATCHED_GLOBS - this
            # write is the user's own, made through the app, not tampering, so
            # re-recording the baseline is what stops it from raising the
            # "your files were modified" banner on the very next launch. Same
            # reasoning as the zapret-update path below.
            integrity.write_manifest(self._project_root)
            self._integrity = integrity.IntegrityReport(verified=True)
            return {"ok": True, "error": None, "count": len(hosts)}
        except Exception as exc:
            # Includes the ValueError naming the offending line number - that
            # message is the whole point of validating at this boundary.
            return {"ok": False, "error": str(exc), "count": 0}

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

    # ---- profiles: carrying them between machines ----

    def export_profiles(self) -> dict:
        """The user's own profiles as JSON text, for the copy-paste path."""
        try:
            payload = export_payload(self._config.profiles)
            return {
                "ok": True,
                "error": None,
                "json": json.dumps(payload, ensure_ascii=False, indent=2),
                "count": len(payload["profiles"]),
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "json": "", "count": 0}

    def import_profiles(self, text: str) -> dict:
        """Add profiles from pasted JSON.

        Everything the import is allowed and not allowed to do lives in
        profiles_io.import_payload - notably that it never overwrites an
        existing profile and never changes which one is active. Here it is
        only persisted."""
        try:
            merged, report = import_payload(text, into=self._config.profiles)
            result = self.update_config({"profiles": merged.model_dump()})
            return {
                "ok": result["ok"],
                "error": result["error"],
                "config": result.get("config"),
                "report": report,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "config": None, "report": None}

    def export_profiles_to_file(self) -> dict:
        """Same export, through the native save dialog. A thin wrapper on
        purpose: the format lives in one place."""
        try:
            if self._window is None:
                return {"ok": False, "error": "окно недоступно", "path": ""}
            chosen = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename="bridgebox-profiles.json",
                file_types=("JSON (*.json)",),
            )
            if not chosen:
                return {"ok": True, "error": None, "path": ""}  # cancelled
            path = Path(chosen[0] if isinstance(chosen, (list, tuple)) else str(chosen))
            exported = self.export_profiles()
            if not exported["ok"]:
                return {"ok": False, "error": exported["error"], "path": ""}
            path.write_text(exported["json"], encoding="utf-8")
            logger.info("exported %d profiles to %s", exported["count"], path)
            return {"ok": True, "error": None, "path": str(path)}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "path": ""}

    def import_profiles_from_file(self) -> dict:
        try:
            if self._window is None:
                return {"ok": False, "error": "окно недоступно", "config": None, "report": None}
            chosen = self._window.create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False, file_types=("JSON (*.json)",)
            )
            if not chosen:
                return {"ok": True, "error": None, "config": None, "report": None}  # cancelled
            path = Path(chosen[0] if isinstance(chosen, (list, tuple)) else str(chosen))
            return self.import_profiles(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"ok": False, "error": str(exc), "config": None, "report": None}

    # ---- zapret update ----

    def check_zapret_update(self) -> dict:
        """Ask Flowseal what the newest release is. Short and blocking."""
        try:
            return self._runtime.run(self._check_update_coro, timeout=25)
        except Exception as exc:
            return {
                "ok": False,
                "error": describe_exception(exc),
                "installed": None,
                "latest": None,
                "updateAvailable": False,
            }

    async def _check_update_coro(self) -> dict:
        zapret_dir = resolve_project_path(self._project_root, self._config.zapret.dir)
        installed = zapret_update.read_installed_version(zapret_dir)
        try:
            async with aiohttp.ClientSession() as session:
                release = await zapret_update.fetch_latest_release(session)
        except Exception as exc:
            # GitHub is routinely unreachable from the networks this app
            # exists for. Say so plainly and point at the one lever the user
            # actually has, rather than leaking a raw aiohttp error.
            return {
                "ok": False,
                "error": (
                    f"не удалось связаться с GitHub ({describe_exception(exc)}). "
                    "Если он у вас заблокирован, добавьте github.com и "
                    "objects.githubusercontent.com в «Домены для обхода» и включите мост."
                ),
                "installed": installed,
                "latest": None,
                "updateAvailable": False,
            }

        return {
            "ok": True,
            "error": None,
            "installed": installed,
            "latest": release.version,
            "updateAvailable": zapret_update.is_newer(release.version, installed),
        }

    def start_startup_update_check(self) -> None:
        """Fire the "Проверять при запуске" check, if it's on.

        Called by main()'s `shown` handler - after the window is painted, not
        before it exists. The check reaches GitHub, which on the networks this
        app exists for is exactly the request that hangs, and starting it
        during startup made a cold launch feel slow for a result nobody is
        waiting on.

        `shown` fires again on every return from the tray, so this guards
        against starting a second run over a live one.

        Fire-and-forget on the same background loop bridge_start/
        test_strategies already use, so a slow or unreachable GitHub (the
        common case on the networks this app exists for - see
        _check_update_coro) never delays the window opening. The frontend
        picks the result up by polling startup_update_check().

        Waits STARTUP_NETWORK_CHECK_DELAY_S before actually reaching GitHub -
        see that constant. startup_update_check() still reports "started" the
        instant this is called, so the frontend's poll loop is not fooled
        into thinking the setting is off during the wait."""
        if not self._config.update.check_on_startup:
            return
        running = self._startup_update_future
        if running is not None and not running.done():
            return

        async def _delayed() -> dict:
            await asyncio.sleep(self._startup_check_delay_s)
            return await self._check_update_coro()

        self._startup_update_future = self._runtime.submit(_delayed)

    def startup_update_check(self) -> dict:
        """Polled by the frontend once on mount, in place of triggering a
        second manual-style check. `started` distinguishes "the setting is
        off" from "still running" - both look like "no result yet" from the
        caller's side otherwise, and only one of them is worth polling
        again for."""
        future = self._startup_update_future
        empty = {"installed": None, "latest": None, "updateAvailable": False}
        if future is None:
            return {"ok": True, "error": None, "started": False, "done": False, **empty}
        if not future.done():
            return {"ok": True, "error": None, "started": True, "done": False, **empty}
        try:
            result = future.result()
        except Exception as exc:
            result = {"ok": False, "error": describe_exception(exc), **empty}
        return {"started": True, "done": True, **result}

    # ---- BridgeBox's own update check ----

    async def _check_app_update_coro(self) -> dict:
        installed = app_version()
        empty = {
            "installed": installed, "latest": None, "notes": None,
            "htmlUrl": None, "critical": False, "updateAvailable": False,
        }
        try:
            async with aiohttp.ClientSession() as session:
                release = await app_update.fetch_latest_release(session)
        except Exception as exc:
            # Same reasoning as _check_update_coro: GitHub is routinely
            # unreachable from the networks this app exists for, and this
            # runs unattended at every startup - a raw aiohttp traceback in
            # the log for that is just noise, not a real error.
            return {"ok": False, "error": describe_exception(exc), **empty}

        return {
            "ok": True,
            "error": None,
            "installed": installed,
            "latest": release.version,
            "notes": release.notes,
            "htmlUrl": release.html_url,
            "critical": release.critical,
            "updateAvailable": app_update.is_newer(release.version, installed),
        }

    def check_app_update(self) -> dict:
        """Manual "Проверить сейчас" - short, blocking, no startup delay."""
        try:
            return self._runtime.run(self._check_app_update_coro, timeout=25)
        except Exception as exc:
            return {
                "ok": False, "error": describe_exception(exc), "installed": app_version(),
                "latest": None, "notes": None, "htmlUrl": None, "critical": False,
                "updateAvailable": False,
            }

    def start_app_update_check(self) -> None:
        """Fire the background release check, if AppUpdateConfig.
        check_on_startup is on. Off by default (see AppUpdateConfig) - a
        fresh install does not phone GitHub until the user opts in, even
        though this is also how a critical security fix would otherwise
        reach somebody who never opens Settings.

        Same shape as start_startup_update_check: called from main()'s
        `shown` handler, waits STARTUP_NETWORK_CHECK_DELAY_S so this does not
        join the logon-time resource scramble on an autostart launch, and
        guards against a second run starting over a live one."""
        if not self._config.app_update.check_on_startup:
            return
        running = self._app_update_future
        if running is not None and not running.done():
            return

        async def _delayed() -> dict:
            await asyncio.sleep(self._startup_check_delay_s)
            return await self._check_app_update_coro()

        self._app_update_future = self._runtime.submit(_delayed)

    def app_update_check(self) -> dict:
        """Polled by the frontend once on mount - same started/done shape as
        startup_update_check(). Also carries `dismissedVersion`, so the
        frontend can decide whether to show the modal for `latest` without a
        second round trip."""
        future = self._app_update_future
        empty = {
            "installed": app_version(), "latest": None, "notes": None,
            "htmlUrl": None, "critical": False, "updateAvailable": False,
        }
        dismissed = self._config.app_update.dismissed_version
        if future is None:
            return {"ok": True, "error": None, "started": False, "done": False,
                     "dismissedVersion": dismissed, **empty}
        if not future.done():
            return {"ok": True, "error": None, "started": True, "done": False,
                     "dismissedVersion": dismissed, **empty}
        try:
            result = future.result()
        except Exception as exc:
            result = {"ok": False, "error": describe_exception(exc), **empty}
        return {"started": True, "done": True, "dismissedVersion": dismissed, **result}

    def dismiss_app_update(self, version: str) -> dict:
        """Remember that the user has already seen and closed the update
        modal for `version`, so it does not reopen on every launch until a
        newer one ships.

        Deliberately does NOT affect the critical banner/reminder (see
        HomeScreen): a critical release must keep nagging even after its
        modal is dismissed once, or "critical" would mean nothing."""
        try:
            self.update_config({"app_update": {"dismissed_version": version}})
            return {"ok": True, "error": None}
        except Exception as exc:
            return {"ok": False, "error": describe_exception(exc)}

    async def _apply_app_update_coro(self) -> dict:
        """Download the newest release's .exe and swap it in for the one
        currently running. Never touches config.yaml, logs/, certs/ or
        zapret/ - only ever writes next to sys.executable, and only that one
        file (see app_update.replace_running_exe). Does NOT restart on its
        own: the frontend shows "Перезапустить сейчас" once this reports
        done, exactly like the zapret update flow already does - restarting a
        pywebview window from a background thread is not something to do
        implicitly."""
        exe_path = app_update.running_exe_path()
        if exe_path is None:
            return {
                "ok": False,
                "error": "Самообновление доступно только в собранной версии BridgeBox "
                "(не в режиме разработки).",
                "version": None,
            }
        stage_path = app_update.stage_path_for(exe_path)
        try:
            archive_path: Path | None = None
            async with aiohttp.ClientSession() as session:
                release = await app_update.fetch_latest_release(session)
                if not release.asset_url:
                    raise RuntimeError(
                        f"release {release.version} ships neither a .exe nor a "
                        "portable .zip to self-update from"
                    )
                if release.asset_is_archive:
                    # The archive is a throwaway, so it goes to the temp
                    # folder. The exe it holds does NOT: replace_running_exe
                    # renames files past each other, which only works within
                    # one volume, and temp is routinely on a different drive
                    # than the app - so the exe is unpacked straight to
                    # stage_path, next to the binary it will replace.
                    archive_path = self._temp_root() / f"BridgeBox-{release.version}.zip"
                    download_target = archive_path
                else:
                    download_target = stage_path
                await app_update.download_exe(session, release.asset_url, download_target)
            try:
                # Runs before the swap, not after: the downloaded bytes get
                # one chance to prove they are what GitHub actually shipped,
                # and a mismatch must never become the app's own running
                # binary - see app_update.verify_exe_digest's own docstring
                # for what this does and does not protect against.
                # Checked against whatever GitHub actually published, which
                # for an archive is the archive - the exe inside inherits
                # that, since it comes out of bytes already proven intact.
                await asyncio.to_thread(
                    app_update.verify_exe_digest, download_target, release.asset_digest
                )
                if archive_path is not None:
                    await asyncio.to_thread(
                        app_update.extract_exe_from_archive, archive_path, stage_path
                    )
            except Exception:
                stage_path.unlink(missing_ok=True)
                raise
            finally:
                if archive_path is not None:
                    archive_path.unlink(missing_ok=True)
            await asyncio.to_thread(app_update.replace_running_exe, stage_path, exe_path)
        except Exception as exc:
            return {"ok": False, "error": describe_exception(exc), "version": None}

        # The exe on disk just changed out from under integrity.py's own
        # baseline (bridgebox.exe is one of WATCHED_GLOBS) - without this,
        # the very next launch would show "files were modified" over a
        # change this process just made itself. Same two lines the zapret
        # update flow already runs after applying its own update.
        await asyncio.to_thread(integrity.write_manifest, self._project_root)
        self._integrity = integrity.IntegrityReport(verified=True)

        logger.info("BridgeBox self-updated to %s - awaiting restart", release.version)
        return {"ok": True, "error": None, "version": release.version}

    def start_app_apply_update(self) -> None:
        """User-triggered ("Обновить сейчас") - fire the download+swap in the
        background. Guards against a second run starting over a live one,
        same as start_app_update_check."""
        running = self._app_apply_future
        if running is not None and not running.done():
            return
        self._app_apply_future = self._runtime.submit(self._apply_app_update_coro)

    def app_apply_progress(self) -> dict:
        """Polled by the frontend after start_app_apply_update()."""
        future = self._app_apply_future
        if future is None:
            return {"started": False, "done": False, "ok": None, "error": None, "version": None}
        if not future.done():
            return {"started": True, "done": False, "ok": None, "error": None, "version": None}
        try:
            result = future.result()
        except Exception as exc:
            result = {"ok": False, "error": describe_exception(exc), "version": None}
        return {"started": True, "done": True, **result}

    def start_zapret_update(self) -> dict:
        """Download and apply in the background - same job shape as
        test_strategies (submit, poll, cancel)."""
        try:
            with self._update_lock:
                if self._update_future is not None and not self._update_future.done():
                    return {"ok": False, "error": "обновление уже выполняется"}
                self._update_state = {
                    "phase": "download",
                    "received": 0,
                    "total": 0,
                    "applied": [],
                    "version": None,
                    "strategies": None,
                }
                self._update_future = self._runtime.submit(self._update_coro)
            return {"ok": True, "error": None}
        except Exception as exc:
            logger.exception("failed to start the zapret update")
            return {"ok": False, "error": describe_exception(exc)}

    def zapret_update_progress(self) -> dict:
        try:
            future = self._update_future
            state = dict(self._update_state)
            done = future is None or future.done()
            error = None
            if future is not None and future.done():
                try:
                    future.result()
                except CancelledError:
                    error = "обновление отменено"
                except Exception as exc:
                    error = describe_exception(exc)
            return {"ok": error is None, "error": error, "done": done, **state}
        except Exception as exc:
            return {"ok": False, "error": describe_exception(exc), "done": True}

    def cancel_zapret_update(self) -> dict:
        try:
            future = self._update_future
            if future is not None and not future.done():
                future.cancel()
                logger.info("zapret update cancelled by user")
            return {"ok": True, "error": None}
        except Exception as exc:
            return {"ok": False, "error": describe_exception(exc)}

    async def _update_coro(self) -> None:
        zapret_dir = resolve_project_path(self._project_root, self._config.zapret.dir)
        temp_root = self._temp_root()

        async with aiohttp.ClientSession() as session:
            release = await zapret_update.fetch_latest_release(session)
            self._update_state["version"] = release.version
            self._update_state["total"] = release.zip_size

            def progress(received: int, total: int) -> None:
                self._update_state["received"] = received
                if total:
                    self._update_state["total"] = total

            archive = await zapret_update.download_archive(
                session, release.zip_url, temp_root / "zapret-release.zip", on_progress=progress
            )

        self._update_state["phase"] = "apply"
        # Everything below is about one thing: nothing may still be holding
        # winws.exe, WinDivert.dll or WinDivert64.sys when install_release
        # starts renaming them.
        #
        # 1. The whole bridge goes down, not just zapret. It owns the sockets
        #    and the WinDivert filter that keep the driver loaded, and the
        #    download is already finished by this point, so there is nothing
        #    left that needs the bypass.
        #
        #    RuntimeCore.stop() is awaited directly rather than going through
        #    BridgeRuntime.stop(): this coroutine is ALREADY running on that
        #    background loop, and run_coroutine_threadsafe(...).result() onto
        #    the loop you are running on is a deadlock.
        await self._runtime_core.stop()
        # 2. This session's zapret process tree.
        zapret = self._runtime_core.zapret_process
        if zapret is not None and zapret.is_running:
            zapret.stop()
        # 3. Then every OTHER winws on the machine. is_running only means "this
        #    session called start()", so one left over from a previous
        #    BridgeBox run - or launched by hand - is invisible above, and it
        #    holds WinDivert64.sys the whole time. The only place in the app
        #    allowed to reach outside its own process tree (see kill_all_winws).
        kill_all_winws()
        # 4. And then WAIT. taskkill returns once it has asked the kernel to
        #    terminate, not once the process is gone - the next line used to
        #    start renaming files while the process holding them was still
        #    being torn down. That is the reported "[WinError 5] Отказано в
        #    доступе" on WinDivert64.sys.
        #    Off the loop thread: this blocks for up to 15s polling tasklist,
        #    and the same loop serves every other Api call.
        if not await asyncio.to_thread(wait_for_winws_exit):
            raise RuntimeError(
                "не удалось завершить winws.exe - обновление отменено, чтобы не "
                "оставить zapret наполовину заменённым. Перезагрузите компьютер "
                "и повторите"
            )
        # 5. The process is gone; the DRIVER it registered is not. WinDivert64.sys
        #    stays loaded until its service stops, and a loaded kernel driver is
        #    the one thing no amount of waiting or retrying releases. `sc stop`
        #    only - never `sc delete`, which would leave it pending-delete and
        #    therefore MORE locked until a reboot. Off the loop thread for the
        #    same reason as the wait above.
        await asyncio.to_thread(stop_windivert_service)

        applied, plan = zapret_update.install_release(
            archive,
            zapret_dir=zapret_dir,
            strategies_dir=self._layout().strategies_dir,
            stage_dir=temp_root,
            version=release.version,
            lang=self.current_language(),
        )
        self._update_state["applied"] = applied
        # Strategies changed too now, so the popup has to be able to say what
        # happened to them - especially "forked", which is the case where the
        # user's own edited file was kept and the new adaptation went beside it.
        self._update_state["strategies"] = {
            "added": plan.added,
            "updated": plan.updated,
            "forked": [list(pair) for pair in plan.forked],
            "skipped": [list(pair) for pair in plan.skipped],
        }
        # The update just replaced files the integrity baseline covers, and
        # those changes are ours. Re-recording here is what stops a successful
        # update from raising the "your files were modified" banner - which
        # would be true, useless, and would teach the user to ignore it.
        await asyncio.to_thread(integrity.write_manifest, self._project_root)
        self._integrity = integrity.IntegrityReport(verified=True)

        self._update_state["phase"] = "done"
        logger.info("zapret updated to %s: %s", release.version, ", ".join(applied))

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

    # ---- Steam launch options ----

    def scan_steam_games(self) -> dict:
        """Read-only, safe to call any time - powers the auto-configure
        checklist in ConnectGuide. Only returns titles that already have an
        appid block in localconfig.vdf (see steam_launch.filter_configurable_games)
        - a title Steam has never launched is excluded, not auto-created.

        "reason" distinguishes the three ways "games" can come back empty -
        Steam not installed, no resolvable active account, or Steam/account
        both fine and there just aren't any eligible titles - so the
        frontend isn't stuck showing "try launching the game once" advice
        for the first two, genuinely different, situations."""
        lang = self.current_language()
        try:
            steam_path = steam_launch.find_steam_path()
            if steam_path is None:
                return {"ok": True, "error": None, "games": [], "reason": i18n.t("steam.not_found", lang)}
            games = steam_launch.scan_installed_jackbox_games(steam_path)
            config_path = steam_launch.find_active_local_config(steam_path)
            if config_path is None:
                return {"ok": True, "error": None, "games": [], "reason": i18n.t("steam.no_active_account", lang)}
            games = steam_launch.filter_configurable_games(config_path, games)
            backups = steam_launch.load_backups(self._project_root)
            return {
                "ok": True, "error": None, "reason": None,
                "games": [
                    {"appid": g.appid, "name": g.name, "hasBackup": g.appid in backups}
                    for g in games
                ],
            }
        except Exception as exc:
            return {"ok": False, "error": describe_exception(exc), "games": [], "reason": None}

    def _localize_steam_result(self, result: dict) -> dict:
        """steam_launch's orchestration functions return error CODES, not
        text - this is the one place that maps them through i18n.t(), same
        separation the diag.* messages already use."""
        lang = self.current_language()
        if result.get("error"):
            result = {**result, "error": i18n.t(f"steam.{result['error']}", lang)}
        results = {
            appid: (
                {**per_game, "error": i18n.t(f"steam.{per_game['error']}", lang)}
                if per_game.get("error") else per_game
            )
            for appid, per_game in result.get("results", {}).items()
        }
        return {**result, "results": results}

    async def _apply_steam_launch_options_coro(self, appids: list[str]) -> dict:
        steam_path = steam_launch.find_steam_path()
        if steam_path is None:
            return {"ok": False, "error": "not_found", "results": {}, "steamRelaunched": False}
        return await asyncio.to_thread(
            steam_launch.apply_launch_options,
            steam_path, self._project_root, appids, self._config.server.port,
        )

    def apply_steam_launch_options(self, appids: list[str]) -> dict:
        if self._steam_launch_busy:
            return {
                "ok": False,
                "error": i18n.t("steam.already_running", self.current_language()),
                "results": {},
                "steamRelaunched": False,
            }
        self._steam_launch_busy = True
        try:
            result = self._runtime.run(
                lambda: self._apply_steam_launch_options_coro(appids),
                timeout=STEAM_LAUNCH_API_TIMEOUT_S,
            )
        except Exception as exc:
            return {"ok": False, "error": describe_exception(exc), "results": {}, "steamRelaunched": False}
        finally:
            self._steam_launch_busy = False
        return self._localize_steam_result(result)

    async def _revert_steam_launch_options_coro(self, appids: list[str]) -> dict:
        steam_path = steam_launch.find_steam_path()
        if steam_path is None:
            return {"ok": False, "error": "not_found", "results": {}, "steamRelaunched": False}
        return await asyncio.to_thread(
            steam_launch.revert_launch_options, steam_path, self._project_root, appids,
        )

    def revert_steam_launch_options(self, appids: list[str]) -> dict:
        if self._steam_launch_busy:
            return {
                "ok": False,
                "error": i18n.t("steam.already_running", self.current_language()),
                "results": {},
                "steamRelaunched": False,
            }
        self._steam_launch_busy = True
        try:
            result = self._runtime.run(
                lambda: self._revert_steam_launch_options_coro(appids),
                timeout=STEAM_LAUNCH_API_TIMEOUT_S,
            )
        except Exception as exc:
            return {"ok": False, "error": describe_exception(exc), "results": {}, "steamRelaunched": False}
        finally:
            self._steam_launch_busy = False
        return self._localize_steam_result(result)

    # ---- Other-copies ("Прочие копии") launch options ----

    def start_other_scan(self) -> dict:
        """Kicks off the drive walk in the background and returns
        immediately - see other_scan_progress. A full scan of every local
        drive can take minutes, long enough that blocking the pywebview
        call would hit its own timeout before finishing (same reasoning as
        test_strategies)."""
        with self._other_scan_lock:
            if self._other_scan_future is not None and not self._other_scan_future.done():
                return {"ok": False, "error": i18n.t("other.already_running", self.current_language())}
            self._other_scan_progress = {"foldersChecked": 0}
            self._other_scan_future = self._runtime.submit(self._other_scan_coro)
        return {"ok": True, "error": None}

    def _on_other_scan_progress(self, folders_checked: int) -> None:
        self._other_scan_progress = {"foldersChecked": folders_checked}

    async def _other_scan_coro(self) -> dict:
        return await asyncio.to_thread(self._run_other_scan)

    def _run_other_scan(self) -> dict:
        drives = other_launch.list_fixed_drives()
        candidates = other_launch.scan_for_other_copies(drives, progress_cb=self._on_other_scan_progress)
        backups = other_launch.load_backups(self._project_root)
        return {
            "items": [
                {"kind": c.kind, "path": c.path, "name": c.name, "hasBackup": c.path in backups}
                for c in candidates
            ],
        }

    def other_scan_progress(self) -> dict:
        """Polled by the frontend after start_other_scan()."""
        future = self._other_scan_future
        progress = dict(self._other_scan_progress)
        if future is None:
            return {"started": False, "done": False, "ok": None, "error": None, "items": [], **progress}
        if not future.done():
            return {"started": True, "done": False, "ok": None, "error": None, "items": [], **progress}
        try:
            result = future.result()
            return {"started": True, "done": True, "ok": True, "error": None, **result, **progress}
        except Exception as exc:
            return {"started": True, "done": True, "ok": False, "error": describe_exception(exc), "items": [], **progress}

    def _localize_other_result(self, result: dict) -> dict:
        """other_launch's orchestration functions return error CODES, not
        text - same separation as _localize_steam_result. No top-level
        result["error"] to translate here (unlike Steam's "could not close
        Steam"/"no active account"): every failure this feature can hit is
        specific to one item, so apply_launch_options/revert_launch_options
        always return a top-level error of None and put the real code in
        results[path]["error"] instead."""
        lang = self.current_language()
        results = {
            path: (
                {**per_item, "error": i18n.t(f"other.{per_item['error']}", lang)}
                if per_item.get("error") else per_item
            )
            for path, per_item in result.get("results", {}).items()
        }
        return {**result, "results": results}

    async def _apply_other_launch_options_coro(self, items: list[dict]) -> dict:
        return await asyncio.to_thread(
            other_launch.apply_launch_options, self._project_root, items, self._config.server.port,
        )

    def apply_other_launch_options(self, items: list[dict]) -> dict:
        if self._other_launch_busy:
            return {"ok": False, "error": i18n.t("other.already_running", self.current_language()), "results": {}}
        self._other_launch_busy = True
        try:
            result = self._runtime.run(
                lambda: self._apply_other_launch_options_coro(items),
                timeout=OTHER_LAUNCH_API_TIMEOUT_S,
            )
        except Exception as exc:
            return {"ok": False, "error": describe_exception(exc), "results": {}}
        finally:
            self._other_launch_busy = False
        return self._localize_other_result(result)

    async def _revert_other_launch_options_coro(self, items: list[dict]) -> dict:
        return await asyncio.to_thread(other_launch.revert_launch_options, self._project_root, items)

    def revert_other_launch_options(self, items: list[dict]) -> dict:
        if self._other_launch_busy:
            return {"ok": False, "error": i18n.t("other.already_running", self.current_language()), "results": {}}
        self._other_launch_busy = True
        try:
            result = self._runtime.run(
                lambda: self._revert_other_launch_options_coro(items),
                timeout=OTHER_LAUNCH_API_TIMEOUT_S,
            )
        except Exception as exc:
            return {"ok": False, "error": describe_exception(exc), "results": {}}
        finally:
            self._other_launch_busy = False
        return self._localize_other_result(result)

    # ---- diagnostics ----

    def test_connection(self) -> dict:
        try:
            return self._runtime.run(self._test_connection_coro, timeout=20)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "steps": []}

    async def _test_connection_coro(self) -> dict:
        """Two checks in one pass:

        1. A plain reachability ping of the real Ecast AND Blobcast hosts
           (ECAST_TARGETS + BLOBCAST_TARGETS - Ecast's API entry point plus a
           relay shard confirmed from live traffic; Blobcast's own API entry
           point alone), so a DPI block shows up immediately as its own step
           instead of being indistinguishable from a room-creation failure
           below. Blobcast is pinged here but not exercised beyond that - the
           room-creation round trip after it is Ecast-only, a much larger
           existing feature this ping addition isn't trying to duplicate.
        2. The room-creation round trip through the bridge itself: create a
           real room via our own /api/v2/rooms (exercises the outbound
           proxy + rewrite), then confirm it registered via
           GET /api/v2/rooms/<code> (both confirmed against the live API).

        Deliberately stops there - no WS relay connect. Confirmed against
        the live API that the actual relay upgrade gets rejected (403) no
        matter what query params/headers accompany it, for reasons still
        unknown; a check that reliably fails for an unrelated, unsolved
        reason is worse than no check, since it reads as "the bridge is
        broken" regardless of whether anything here actually is.

        Uses TEST_APPTAG ("fourbage", confirmed active against live traffic)
        plus a fresh userId (the create call is rejected outright without
        one - confirmed against the live API, previously silently missing
        here since every earlier check of this path used a fake upstream
        that never enforced it)."""
        import ssl

        lang = self.current_language()
        steps: list[str] = []
        status = self._runtime.get_status()
        if not status.get("running"):
            return {"ok": False, "error": i18n.t("diag.bridge_not_running", lang), "steps": steps}

        port = status["port"]
        # Verified against our own CA rather than CERT_NONE. The bridge's leaf
        # carries 127.0.0.1 as a SAN and the CA is right there on disk, so
        # turning verification off bought nothing and quietly meant "проверить
        # соединение" could not have noticed a broken certificate - which is
        # one of the things it exists to check.
        cert_dir = resolve_project_path(
            self._project_root, self._config.server.tls.cert_dir
        )
        ca_file = cert_dir / CA_CERT_FILENAME
        if ca_file.exists():
            ssl_context = ssl.create_default_context(cafile=str(ca_file))
        else:
            # The bridge is running, so the CA should exist; if it somehow does
            # not, say so instead of silently downgrading to no verification.
            steps.append(i18n.t("diag.no_ca_file", lang, name=ca_file.name))
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        # The profile the bridge is actually serving, not the legacy top-level
        # `rewrite` section - see config.rewrite_for.
        rewrite = rewrite_for(self._config.profiles.active("ecast"))

        async with aiohttp.ClientSession() as session:
            # Purely informational - a failed ping doesn't stop the test, so
            # the room-creation step below still runs and can show whether
            # the API host specifically is reachable even if the relay shard
            # isn't (or vice versa).
            ping_results = await probe_targets(session, ECAST_TARGETS + BLOBCAST_TARGETS)
            for name, result in ping_results.items():
                if result["ok"]:
                    steps.append(
                        i18n.t(
                            "diag.ping_ok",
                            lang,
                            name=name,
                            status=result["status"],
                            ms=f"{result['elapsedMs']:.0f}",
                        )
                    )
                else:
                    steps.append(i18n.t("diag.ping_error", lang, name=name, error=result["error"]))

            # A browser-like User-Agent is mandatory, not cosmetic: Jackbox's
            # AWS load balancer answers anything else with a 403 HTML page
            # before the request reaches Ecast (see rooms.py). aiohttp sends
            # its own "Python/3.x aiohttp/3.x" by default, and the proxy
            # forwards a UA that is already present rather than substituting
            # the fallback - so this test was reliably getting HTML back and
            # reporting the resulting JSONDecodeError as "ошибка сети".
            # Confirmed against the live API just now: a create-room request
            # with no userId is rejected outright - {"ok": false, "error":
            # "invalid parameters: missing required field userId"} - which
            # this test was silently sending until now (every earlier
            # verification of this code path used a fake upstream that never
            # enforced the real server's required fields). A fresh id per
            # run, same shape Jackbox's own client generates.
            user_id = str(uuid.uuid4()).upper()
            try:
                async with session.post(
                    f"https://127.0.0.1:{port}/api/v2/rooms",
                    json={"apptag": TEST_APPTAG, "userId": user_id},
                    headers={"User-Agent": rewrite.fallback_user_agent},
                    ssl=ssl_context,
                ) as response:
                    status = response.status
                    raw = await response.read()
            except Exception as exc:
                steps.append(
                    i18n.t("diag.create_room_network_error", lang, detail=describe_exception(exc))
                )
                return {"ok": False, "error": steps[-1], "steps": steps}

            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Report what actually came back instead of a bare parse
                # error - HTTP status plus a body snippet is the difference
                # between "DPI is blocking us" and "the API changed shape".
                snippet = redact(raw[:200].decode("utf-8", errors="replace").strip())
                steps.append(
                    i18n.t(
                        "diag.create_room_not_json",
                        lang,
                        status=status,
                        n=len(raw),
                        snippet=repr(snippet),
                    )
                )
                return {"ok": False, "error": steps[-1], "steps": steps}

            if not isinstance(body, dict):
                steps.append(
                    i18n.t(
                        "diag.create_room_unexpected_json",
                        lang,
                        status=status,
                        json=_redacted_json(body)[:200],
                    )
                )
                return {"ok": False, "error": steps[-1], "steps": steps}

            # Reuses the exact function RoomsProxy uses in production, rather
            # than re-checking body.get("roomid") here: the real API wraps
            # the payload in {"ok":..., "body": {...}} and names the room
            # code "code", not "roomid" - a flat top-level lookup for those
            # two keys reported "no room" on every real room creation, which
            # is what "ответ без комнаты/relay" actually was.
            local_ws_base = f"wss://127.0.0.1:{port}/ws"
            _, server, room_id = rewrite_server_field(
                raw, local_ws_base=local_ws_base, rewrite=rewrite
            )

            if not room_id:
                steps.append(
                    i18n.t(
                        "diag.create_room_no_code",
                        lang,
                        status=status,
                        keys=tuple(rewrite.room_id_keys),
                        json=_redacted_json(body)[:400],
                    )
                )
                return {"ok": False, "error": steps[-1], "steps": steps}

            # Whether a "server"/"host" relay field was found and rewritten
            # is purely informational here - this test no longer opens a WS
            # relay connection (see the coro's docstring), so a room lacking
            # one is not a failure, just a note about this particular app.
            relay_note = f", relay -> {server}" if server else ""
            steps.append(
                i18n.t("diag.room_created", lang, room_id=room_id, status=status, relay_note=relay_note)
            )

            # Confirmed against the live API: GET /api/v2/rooms/<code> right
            # after creation returns 200 with the room's full state (host,
            # audienceHost, locked, full, maxPlayers, ...) - a real
            # second-step lookup a game client can rely on, not just a POST
            # response the room could theoretically forget. A failure here
            # means the room didn't actually register server-side, which the
            # POST response alone can't tell you.
            try:
                async with session.get(
                    f"https://127.0.0.1:{port}/api/v2/rooms/{room_id}",
                    headers={"User-Agent": rewrite.fallback_user_agent},
                    ssl=ssl_context,
                ) as lookup_response:
                    lookup_status = lookup_response.status
                    await lookup_response.read()
            except Exception as exc:
                steps.append(
                    i18n.t(
                        "diag.room_check_network_error",
                        lang,
                        room_id=room_id,
                        detail=describe_exception(exc),
                    )
                )
                return {"ok": False, "error": steps[-1], "steps": steps}

            if lookup_status != 200:
                steps.append(
                    i18n.t("diag.room_check_bad_status", lang, room_id=room_id, status=lookup_status)
                )
                return {"ok": False, "error": steps[-1], "steps": steps}
            steps.append(i18n.t("diag.room_confirmed", lang, room_id=room_id))

            await self._close_test_room(
                session,
                port,
                room_id,
                ssl_context,
                steps,
                token=_find_key(body, "token"),
                user_agent=rewrite.fallback_user_agent,
            )

        return {"ok": True, "error": None, "steps": steps}

    async def _close_test_room(
        self,
        session,
        port,
        room_id,
        ssl_context,
        steps: list,
        *,
        token: str | None = None,
        user_agent: str,
    ) -> None:
        """Tear the test room back down. The PRD asked for "создание комнаты/
        разрушение комнаты"; only the create half was ever implemented, so
        every click of "Проверить соединение" left a real room behind on
        Jackbox's own servers. They very likely expire on their own (nothing
        ever opens a host WS to them), but creating them by the dozen and
        never cleaning up is load on someone else's infrastructure that this
        diagnostic has no business generating.

        Strictly best-effort, and deliberately never fails the test: by the
        time this runs the check has already proved everything it set out to.

        The token goes in the QUERY STRING, not a header. That is measured,
        not assumed: probed against the real API, `Authorization: Bearer
        <token>` and a bare `Authorization: <token>` both answer
        403 {"ok":false,"error":"bad token"}, and `?token=<token>` answers
        200 "ok". Every room this diagnostic created before that was found
        stayed open on Jackbox's servers - 12 real attempts in the log, zero
        successes."""
        lang = self.current_language()
        headers = {"User-Agent": user_agent}
        params = {"token": token} if token else {}

        try:
            async with session.delete(
                f"https://127.0.0.1:{port}/api/v2/rooms/{room_id}",
                headers=headers,
                params=params,
                ssl=ssl_context,
            ) as response:
                status = response.status
                raw = await response.read()
        except Exception as exc:
            steps.append(
                i18n.t(
                    "diag.room_close_failed_network",
                    lang,
                    room_id=room_id,
                    detail=describe_exception(exc),
                )
            )
            logger.warning("could not close test room %s: %s", room_id, exc)
            return

        if 200 <= status < 300:
            steps.append(i18n.t("diag.room_closed", lang, room_id=room_id, status=status))
            return

        # Report what the server actually said. Saying "not supported" here
        # was wrong: the server's own answer was "bad token", which is a
        # different problem with a different fix.
        detail = redact(raw[:120].decode("utf-8", errors="replace").strip())
        steps.append(
            i18n.t("diag.room_close_failed_status", lang, room_id=room_id, status=status, detail=detail)
        )
        logger.info("DELETE of test room %s answered HTTP %s: %s", room_id, status, detail)

    def test_strategies(self, skip_heavy: bool = True, target_set: str = "ecast") -> dict:
        """Start the suite in the background and return immediately. A full
        run takes minutes - blocking the pywebview call for that long meant a
        UI timeout discarded every result that had already been measured, so
        the popup showed nothing. The frontend polls test_strategies_progress()
        and calls test_strategies_cancel() when the popup closes.

        target_set is "ecast", "blobcast", or "both" - which protocol's hosts
        every strategy gets probed against. "both" is two complete passes run
        back to back (see _stages_for), not one pass against four combined
        targets: a strategy that helps one protocol and hurts the other would
        be unreadable averaged into a single row, and the fastest-strategy
        badge would be picking a winner across two different networks."""
        try:
            if target_set not in ("ecast", "blobcast", "both"):
                return {"ok": False, "error": f"неизвестный набор целей: {target_set!r}", "total": 0}

            # The whole check-then-start sequence is under one lock: the guard
            # and the assignment to _strategy_future used to be a dozen lines
            # apart, so two calls arriving together both saw "not running" and
            # started two suites, which then raced each other stopping and
            # starting the same winws.exe. The UI disables the button during a
            # run, but that is not where the invariant belongs. Held across the
            # local directory scan too - this is a button press, not a hot path.
            with self._strategy_lock:
                if self._strategy_future is not None and not self._strategy_future.done():
                    return {"ok": False, "error": "тест стратегий уже выполняется", "total": 0}

                strategies = discover_strategies(self._layout().strategies_dir)
                # group_strategies already yields Основная -> Альтернативы ->
                # Прочие (matching how Settings displays them), rather than
                # discover_strategies()'s alphabetical-by-filename order (which
                # would test "Alternative 1" before "General").
                ordered = [s for items in group_strategies(strategies).values() for s in items]
                if skip_heavy:
                    ordered = [s for s in ordered if s.group != "Прочие"]

                stages = _stages_for(target_set)
                self._strategy_results = []
                self._strategy_error = None
                self._strategy_stage = stages[0][0]
                self._strategy_future = self._runtime.submit(
                    lambda: self._test_strategies_coro(ordered, strategies, stages)
                )
            total = len(ordered) * len(stages)
            logger.info(
                "strategy suite started: %d strategies queued x %d stage(s) (%s)",
                len(ordered),
                len(stages),
                target_set,
            )
            return {"ok": True, "error": None, "total": total}
        except Exception as exc:
            logger.exception("failed to start strategy suite")
            return {"ok": False, "error": describe_exception(exc), "total": 0}

    def test_strategies_progress(self) -> dict:
        """Poll for results measured so far. Safe to call at any time - a run
        that was never started just reports done with no results."""
        try:
            future = self._strategy_future
            # list() snapshots under the GIL; the suite only ever appends.
            results = list(self._strategy_results)
            error = self._strategy_error
            done = future is None or future.done()

            if future is not None and future.done():
                try:
                    future.result()
                except CancelledError:
                    error = error or "тест отменён"
                except Exception as exc:
                    error = error or describe_exception(exc)

            return {
                "ok": error is None,
                "error": error,
                "results": results,
                "fastestKey": _fastest_key(results),
                # Which of "ecast"/"blobcast" is running right now; None once
                # the run finishes or before one has started. "both" walks
                # through both values in turn - see _test_strategies_coro.
                "stage": self._strategy_stage,
                "done": done,
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": describe_exception(exc),
                "results": [],
                "fastestKey": None,
                "stage": None,
                "done": True,
            }

    def test_strategies_cancel(self) -> dict:
        """Stop an in-flight suite and put Zapret back the way the user had
        it. Closing the popup used to leave winws.exe running on whichever
        strategy the test happened to reach."""
        try:
            future = self._strategy_future
            if future is not None and not future.done():
                future.cancel()
                logger.info("strategy suite cancelled by user")
            self._restore_configured_zapret()
            return {"ok": True, "error": None}
        except Exception as exc:
            logger.exception("failed to cancel strategy suite")
            return {"ok": False, "error": describe_exception(exc)}

    def export_strategy_results(self, fmt: str) -> dict:
        """Export the most recently run (or still-running) suite via the
        native save dialog. Reads self._strategy_results directly rather than
        taking it as an argument - it's the same list test_strategies_progress()
        has been streaming to the popup, so the frontend never has to round-
        trip its own copy back over the bridge just to save it."""
        try:
            if fmt not in ("json", "html"):
                return {"ok": False, "error": f"неизвестный формат: {fmt!r}", "path": ""}
            if self._window is None:
                return {"ok": False, "error": "окно недоступно", "path": ""}
            if not self._strategy_results:
                return {"ok": False, "error": "нет результатов теста стратегий", "path": ""}

            if fmt == "json":
                content = render_strategy_results_json(self._strategy_results)
                default_name, file_types = "bridgebox-strategy-test.json", ("JSON (*.json)",)
            else:
                content = render_strategy_results_html(self._strategy_results)
                default_name, file_types = "bridgebox-strategy-test.html", ("HTML (*.html)",)

            chosen = self._window.create_file_dialog(
                webview.SAVE_DIALOG, save_filename=default_name, file_types=file_types
            )
            if not chosen:
                return {"ok": True, "error": None, "path": ""}  # cancelled
            path = Path(chosen[0] if isinstance(chosen, (list, tuple)) else str(chosen))
            path.write_text(content, encoding="utf-8")
            logger.info(
                "exported %d strategy-test results (%s) to %s", len(self._strategy_results), fmt, path
            )
            return {"ok": True, "error": None, "path": str(path)}
        except Exception as exc:
            return {"ok": False, "error": describe_exception(exc), "path": ""}

    async def _test_strategies_coro(self, ordered, strategies, stages) -> None:
        switch = build_switch(
            self._runtime_core.zapret_process,
            strategies,
            hide_console=self._config.zapret.hide_console,
        )
        try:
            for stage_name, targets in stages:
                self._strategy_stage = stage_name

                def on_result(entry: dict, _stage: str = stage_name) -> None:
                    # Tagged per row rather than assumed from context: "both"
                    # interleaves ecast and blobcast rows in one flat list, and
                    # the popup (and the HTML/JSON export) needs to tell them
                    # apart to render two separate tables.
                    self._strategy_results.append({**entry, "targetSet": _stage})

                await run_strategy_suite(
                    ordered,
                    switch=switch,
                    session_factory=aiohttp.ClientSession,
                    targets=targets,
                    on_result=on_result,
                )
        finally:
            # Runs on normal completion AND on cancellation, so the machine is
            # never left with a test strategy still active.
            self._strategy_stage = None
            self._restore_configured_zapret()

    def _restore_configured_zapret(self) -> None:
        """Return Zapret to the user's configured strategy if the bridge is
        up, otherwise leave it stopped. Never raises - this is cleanup."""
        zapret = self._runtime_core.zapret_process
        if zapret is None:
            return
        try:
            if zapret.is_running:
                zapret.stop()
            if not self._runtime.get_status().get("running"):
                return
            strategies = discover_strategies(self._layout().strategies_dir)
            strategy = resolve_strategy(self._config.zapret.strategy, strategies)
            hide_console = self._config.zapret.hide_console
            zapret.start(
                strategy.path,
                creationflags=console_flags(hide_console),
                capture_output=hide_console,
            )
            logger.info("zapret restored to configured strategy %s", strategy.key)
        except Exception as exc:
            logger.error("could not restore configured zapret strategy: %s", exc)

    # ---- logs ----

    def get_log_lines(self, since_seq: int = 0, limit: int = 500) -> dict:
        try:
            result = self._log_buffer.since(since_seq, limit)
            return {"ok": True, "error": None, **result}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "lines": [], "nextSeq": since_seq}

    def export_logs(self, fmt: str) -> dict:
        """Save the whole buffer through the native save dialog.

        Reads the buffer rather than taking the frontend's filtered copy: an
        export is for a bug report, and a report missing whatever the level
        pills happened to be hiding is worse than useless. Same dialog shape as
        export_strategy_results - one pattern for every file this app writes."""
        try:
            if fmt not in EXPORT_FORMATS:
                return {"ok": False, "error": f"неизвестный формат: {fmt!r}", "path": ""}
            if self._window is None:
                return {"ok": False, "error": "окно недоступно", "path": ""}

            lines = self._log_buffer.snapshot()
            if not lines:
                return {"ok": False, "error": "лог пуст", "path": ""}

            file_types = {
                "log": ("Журнал (*.log)",),
                "json": ("JSON (*.json)",),
                "html": ("HTML (*.html)",),
            }[fmt]
            chosen = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=f"bridgebox-logs.{fmt}",
                file_types=file_types,
            )
            if not chosen:
                return {"ok": True, "error": None, "path": ""}  # cancelled
            path = Path(chosen[0] if isinstance(chosen, (list, tuple)) else str(chosen))
            path.write_text(render_log(lines, fmt), encoding="utf-8")
            logger.info("exported %d log lines (%s) to %s", len(lines), fmt, path)
            return {"ok": True, "error": None, "path": str(path)}
        except Exception as exc:
            logger.exception("log export failed")
            return {"ok": False, "error": describe_exception(exc), "path": ""}


def _stages_for(target_set: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """Which (name, targets) passes test_strategies runs, in order.

    "both" is genuinely two full passes over every strategy, not one pass
    against four combined targets - Api.test_strategies's docstring explains
    why a strategy that helps one protocol and hurts the other needs to stay
    readable as two rows, not get averaged into one."""
    if target_set == "ecast":
        return [("ecast", ECAST_TARGETS)]
    if target_set == "blobcast":
        return [("blobcast", BLOBCAST_TARGETS)]
    return [("ecast", ECAST_TARGETS), ("blobcast", BLOBCAST_TARGETS)]


def _fastest_key(results: list[dict]) -> str | None:
    ok_results = [r for r in results if r["ok"]]
    if not ok_results:
        return None

    def _best_time(r: dict) -> float:
        times = [t["elapsedMs"] for t in r["targets"].values() if t["ok"]]
        return min(times) if times else float("inf")

    return min(ok_results, key=_best_time)["key"]


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

    # Leftover from a self-update that swapped the .exe on a previous run, or
    # one that downloaded but never finished (see app_update.replace_running_exe
    # / verify_exe_digest) - the old image could not be deleted while that
    # process still held it, so this is where it finally goes. Frozen-only
    # (running_exe_path() is None in dev) and best-effort.
    exe_path = app_update.running_exe_path()
    if exe_path is not None:
        try:
            app_update.cleanup_stale_files(exe_path)
        except Exception:
            logger.exception("could not clean up leftover self-update files - continuing")

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
