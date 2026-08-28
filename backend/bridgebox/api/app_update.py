"""BridgeBox's own self-update: check, download+swap the running .exe, and
report progress. See desktop.py's own docstring on the mixin split. Not to be
confused with bridgebox/app_update.py, the module this borrows
fetch_latest_release/download_exe/... from - that one holds the release/exe
mechanics, this one is just its Api wrapper."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil

import aiohttp

from .. import app_update
from .. import integrity
from ..diagnostics import describe_exception
from ..version import app_version

logger = logging.getLogger(__name__)


class AppUpdateMixin:
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
        """Download the newest release's portable .zip and swap BOTH halves
        of the running onedir install in for it - bridgebox.exe and
        _internal/ (see app_update.replace_running_exe /
        replace_running_internal). Never touches config.yaml, logs/, certs/
        or zapret/ - only ever writes those two, next to sys.executable.
        Does NOT restart on its own: the frontend shows "Перезапустить
        сейчас" once this reports done, exactly like the zapret update flow
        already does - restarting a pywebview window from a background
        thread is not something to do implicitly."""
        exe_path = app_update.running_exe_path()
        internal_path = app_update.running_internal_dir()
        if exe_path is None or internal_path is None:
            return {
                "ok": False,
                "error": "Самообновление доступно только в собранной версии BridgeBox "
                "(не в режиме разработки).",
                "version": None,
            }
        exe_stage = app_update.stage_path_for(exe_path)
        internal_stage = app_update.stage_path_for(internal_path)
        try:
            # The archive is a throwaway, so it goes to the temp folder. The
            # staged exe/_internal do NOT: the swap below renames paths past
            # each other, which only works within one volume, and temp is
            # routinely on a different drive than the app - so both are
            # unpacked straight to their stage paths, next to what they will
            # replace.
            archive_path = self._temp_root() / "BridgeBox-release.zip"
            async with aiohttp.ClientSession() as session:
                release = await app_update.fetch_latest_release(session)
                if not release.asset_url or not release.asset_is_archive:
                    raise RuntimeError(
                        f"release {release.version or '?'} ships no portable .zip to "
                        "self-update from"
                    )
                await app_update.download_exe(session, release.asset_url, archive_path)
            try:
                # Runs before the swap, not after: the downloaded bytes get
                # one chance to prove they are what GitHub actually shipped,
                # and a mismatch must never become the app's own running
                # install - see app_update.verify_exe_digest's own docstring
                # for what this does and does not protect against. Checked
                # against the whole zip, which is what GitHub's own digest
                # covers - the exe and _internal/ extracted from it inherit
                # that, since they come out of bytes already proven intact.
                await asyncio.to_thread(
                    app_update.verify_exe_digest, archive_path, release.asset_digest
                )
                await asyncio.to_thread(
                    app_update.extract_release_from_archive,
                    archive_path, exe_stage, internal_stage,
                )
            except Exception:
                exe_stage.unlink(missing_ok=True)
                shutil.rmtree(internal_stage, ignore_errors=True)
                raise
            finally:
                archive_path.unlink(missing_ok=True)

            # Exe, then _internal/ - both must land or neither does. If the
            # second swap fails after the first already succeeded (a crash,
            # a full disk - the swaps themselves already retry a mere lock),
            # roll the exe back too rather than persist a new exe paired
            # with an old _internal/, which the coro's own docstring already
            # explains cannot run.
            exe_backup = await asyncio.to_thread(
                app_update.replace_running_exe, exe_stage, exe_path
            )
            try:
                await asyncio.to_thread(
                    app_update.replace_running_internal, internal_stage, internal_path
                )
            except Exception:
                try:
                    os.replace(exe_backup, exe_path)
                except OSError:
                    logger.exception(
                        "could not roll the exe back after a failed _internal/ swap - "
                        "install may now be inconsistent"
                    )
                raise
        except Exception as exc:
            return {"ok": False, "error": describe_exception(exc), "version": None}

        # The install on disk just changed out from under integrity.py's own
        # baseline (bridgebox.exe and _internal/**/* are both WATCHED_GLOBS)
        # - without this, the very next launch would show "files were
        # modified" over a change this process just made itself. Same two
        # lines the zapret update flow already runs after applying its own
        # update.
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
