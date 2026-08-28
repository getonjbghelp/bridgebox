"""Steam launch-options - auto-configure Jackbox titles in localconfig.vdf.
See desktop.py's own docstring on the mixin split."""
from __future__ import annotations

import asyncio

from .. import i18n
from .. import steam_launch
from ..diagnostics import describe_exception

# steam_launch.quit_steam's own worst case: a `-shutdown` subprocess call, a
# poll loop that runs up to _GRACEFUL_QUIT_TIMEOUT_S, and a forced `taskkill`
# call - each of those subprocess calls has its own 10s timeout - before the
# file is ever touched. Derived from the module's own constant (times 3 for
# the three subprocess timeouts, plus real margin) rather than a bare magic
# number, so the two can't silently drift apart again: a `timeout=30` here
# used to let the Api layer report failure while quit_steam kept running in
# the background and the file still got rewritten underneath the user.
STEAM_LAUNCH_API_TIMEOUT_S = steam_launch._GRACEFUL_QUIT_TIMEOUT_S * 3 + 60


class SteamLaunchMixin:
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
