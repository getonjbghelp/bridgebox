"""Autostart, self-integrity, and the CA certificate - the "is this machine
set up right" domain. See desktop.py's own docstring on the mixin split."""
from __future__ import annotations

import logging
import threading

from .. import integrity
from ..autostart import disable as disable_autostart
from ..autostart import enable as enable_autostart
from ..autostart import is_enabled as autostart_is_enabled
from ..diagnostics import describe_exception
from ..window_chrome import THEMED_NONE, apply_titlebar_theme

logger = logging.getLogger(__name__)


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
