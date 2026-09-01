"""Autostart, self-integrity, and the CA certificate - the "is this machine
set up right" domain. See desktop.py's own docstring on the mixin split."""
from __future__ import annotations

import logging
import threading
import time

from .. import integrity
from ..autostart import disable as disable_autostart
from ..autostart import enable as enable_autostart
from ..autostart import is_enabled as autostart_is_enabled
from ..diagnostics import describe_exception
from ..window_chrome import THEMED_NONE, apply_titlebar_theme

logger = logging.getLogger(__name__)

# Fallback only - see notify_ui_settled for the real trigger. The window
# fires `shown` before it has drawn much of anything, and the first seconds
# after that are when the user is clicking around the tabs for the first
# time - exactly when a motion trace showed frames taking 220ms with the
# renderer's own main thread idle and no script running. Nothing was
# computing the new screen; it just could not get a frame presented. Every
# screen switch after the first few seconds, including the first ever
# display of Settings 79 seconds in, was clean - so what mattered was WHEN,
# not which screen. Nobody is waiting on this result: it feeds a banner
# about a warning that cannot be acted on instantly anyway. This still has
# to cover notify_ui_settled never arriving at all (a browser dev session
# with no bridge, a JS error before the App.tsx effect runs), so it stays a
# blind, generous wait rather than shrinking to match the happy path.
STARTUP_INTEGRITY_DELAY_S = 10

# The real trigger: App.tsx's prewarm effect already measures the moment
# that matters - all three screens have had their first (expensive) layout
# and paint, so a screen switch right after this is the cheap ~15ms kind,
# not the 66-76ms first-visit kind that used to land in the same seconds as
# the hash. The disk contention this whole delay dance exists to avoid has
# much less to collide with by the time this fires, which is why it can be
# short instead of a second copy of the 10s guess above - a small buffer,
# not 0, because the settle signal measures layout work, not disk idleness.
UI_SETTLED_INTEGRITY_DELAY_S = 2

# Sleep briefly every this many files while hashing - see
# integrity.build_manifest's yield_every. The delay above keeps this off the
# first interactions; this keeps it from monopolising the disk whenever it
# does run, including on a restore from the tray.
INTEGRITY_YIELD_EVERY = 25


class SystemMixin:
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

    def notify_ui_settled(self) -> None:
        """Called once by App.tsx right after its own prewarm-off signal - a
        real measurement of "the UI is done with its expensive first layout"
        instead of the blind STARTUP_INTEGRITY_DELAY_S guess start_integrity_
        check falls back to when this never arrives. Whichever of the two
        fires first wins; _start_integrity_check's guard covers the rest."""
        self._start_integrity_check(delay_s=UI_SETTLED_INTEGRITY_DELAY_S)

    def start_integrity_check(self) -> None:
        """Fallback trigger, called from on_shown - see notify_ui_settled for
        the one that normally wins."""
        self._start_integrity_check(delay_s=self._integrity_delay_s)

    def _start_integrity_check(self, *, delay_s: float) -> None:
        """Hash our own files once, in the background, out of the UI's way.

        A thread, not the event loop: this is blocking disk I/O over a few
        hundred files, and the loop serves every other Api call.

        Guarded by _integrity_lock rather than the old `self._integrity is
        not None` check, which only ruled out a SECOND run once the first had
        already finished - two callers racing to start the first one (the
        normal case now that there are two entry points, and already possible
        before this if `shown` fired twice from a fast tray restore) could
        both win that check while the report was still None. The lock makes
        "have I already started" true the instant the winner claims it.

        It also waits before it starts and yields while it runs, both for the
        same reason - see STARTUP_INTEGRITY_DELAY_S. The timing is logged
        because the reasoning behind those numbers is a measurement, and a
        measurement that cannot be checked again later is one that rots."""
        with self._integrity_lock:
            if self._integrity_started:
                return
            self._integrity_started = True

        def run() -> None:
            time.sleep(delay_s)
            started = time.monotonic()
            report = integrity.ensure_baseline(
                self._project_root, yield_every=INTEGRITY_YIELD_EVERY
            )
            logger.info(
                "integrity check finished in %.2fs (started %.1fs after trigger)",
                time.monotonic() - started,
                delay_s,
            )
            self._integrity = report

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
