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
import subprocess

import aiohttp

from .. import app_update
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

    async def _changelog_coro(self) -> dict:
        try:
            async with aiohttp.ClientSession() as session:
                releases = await app_update.fetch_releases(session)
        except Exception as exc:
            return {"ok": False, "error": describe_exception(exc), "releases": []}

        return {
            "ok": True,
            "error": None,
            "releases": [
                {
                    "version": r.version,
                    "name": r.name,
                    "body": r.body,
                    "date": r.date,
                    "htmlUrl": r.html_url,
                }
                for r in releases
            ],
        }

    def changelog(self) -> dict:
        """The Info screen's "История версий" - GitHub's release list, raw.

        Bilingual title/level parsing (the "«Название» • MINOR/MAJOR/CRITICAL"
        convention, see lib/content.ts on the frontend) and the pre-0.1.6
        local fallback both live in the frontend, next to the RU/EN body
        splitting it already owned (lib/releaseNotes.ts) - this stays a plain
        fetch, same division of labour as check_app_update/_check_app_update_coro
        above."""
        try:
            return self._runtime.run(self._changelog_coro, timeout=25)
        except Exception as exc:
            return {"ok": False, "error": describe_exception(exc), "releases": []}

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
        """Download the newest release's portable .zip and stage BOTH halves
        of the running onedir install - bridgebox.exe and _internal/ - at
        their `.new` paths (app_update.stage_path_for). Does NOT touch the
        live install: see app_update's own module docstring for why the
        actual swap has to happen from a relaunch script instead, after this
        process has exited, rather than here. The frontend shows
        "Перезапустить сейчас" once this reports done, same as the zapret
        update flow already does - restart_after_app_update is what that
        button actually calls."""
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
            # staged exe/_internal do NOT: the relaunch script renames paths
            # past each other, which only works within one volume, and temp
            # is routinely on a different drive than the app - so both are
            # unpacked straight to their stage paths, next to what they will
            # eventually replace.
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
                # Runs before extraction, not after: the downloaded bytes get
                # one chance to prove they are what GitHub actually shipped,
                # and a mismatch must never become a staged install waiting
                # for the next restart to apply it - see
                # app_update.verify_exe_digest's own docstring for what this
                # does and does not protect against. Checked against the
                # whole zip, which is what GitHub's own digest covers - the
                # exe and _internal/ extracted from it inherit that, since
                # they come out of bytes already proven intact.
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
        except Exception as exc:
            return {"ok": False, "error": describe_exception(exc), "version": None}

        # Deliberately no integrity.write_manifest here: the files on disk
        # have not changed yet, only staged copies exist. Recording a
        # baseline now would describe files that are about to be replaced -
        # main() does this instead, on the next launch, only once
        # cleanup_stale_files confirms a swap actually happened.
        logger.info("BridgeBox update to %s staged - awaiting restart to apply", release.version)
        return {"ok": True, "error": None, "version": release.version}

    def restart_after_app_update(self) -> dict:
        """"Перезапустить сейчас" for a self-update - writes and launches the
        detached relaunch script that performs the actual swap once this
        process is gone (see app_update.build_relaunch_script and its
        module's own docstring for why), then shuts this process down the
        same way restart_app() does.

        Refuses if _apply_app_update_coro never staged anything (or a prior
        restart already consumed the stage) - nothing to apply and nothing
        this process should exit for."""
        exe_path = app_update.running_exe_path()
        internal_path = app_update.running_internal_dir()
        if exe_path is None or internal_path is None:
            return {
                "ok": False,
                "error": "Самообновление доступно только в собранной версии BridgeBox.",
            }
        exe_stage = app_update.stage_path_for(exe_path)
        internal_stage = app_update.stage_path_for(internal_path)
        if not exe_stage.exists() or not internal_stage.is_dir():
            return {
                "ok": False,
                "error": "Нет подготовленного обновления для установки - "
                "сначала нажмите «Обновить BridgeBox».",
            }
        try:
            script = app_update.build_relaunch_script(
                pid=os.getpid(),
                exe_path=exe_path,
                exe_stage=exe_stage,
                internal_path=internal_path,
                internal_stage=internal_stage,
            )
            script_path = self._temp_root() / "bridgebox_apply_update.bat"
            script_path.parent.mkdir(parents=True, exist_ok=True)
            script_path.write_text(script, encoding="utf-8")
            subprocess.Popen(
                ["cmd", "/c", str(script_path)],
                cwd=str(self._project_root),
                creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            logger.info("relaunch helper started (%s) - stopping to let it apply the update", script_path)
        except OSError as exc:
            logger.exception("could not start the relaunch helper")
            return {"ok": False, "error": describe_exception(exc)}

        # Same shutdown shape as desktop.Api.restart_app: stop the bridge
        # gracefully, then close the window - the helper is already waiting
        # on this process's PID and will not touch anything until it is
        # actually gone.
        self._runtime.stop()
        if self._window is not None:
            self._window.destroy()
        return {"ok": True, "error": None}

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
