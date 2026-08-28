"""Non-Steam ("Прочие копии") launch-options - the drive-scan + patch flow for
Jackbox copies Steam doesn't know about. See desktop.py's own docstring on
the mixin split."""
from __future__ import annotations

import asyncio

from .. import i18n
from .. import other_launch
from ..diagnostics import describe_exception

# No process to close first (unlike Steam) - patching a handful of files/
# shortcuts via COM is a matter of seconds even on a slow disk.
OTHER_LAUNCH_API_TIMEOUT_S = 60


class OtherLaunchMixin:
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
