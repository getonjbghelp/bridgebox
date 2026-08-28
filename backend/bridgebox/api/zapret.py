"""Zapret: strategy discovery/listing, the hostlist editor, the Flowseal
release-update flow, and the strategy-test suite (start/poll/cancel/export).
The largest domain - see desktop.py's own docstring on the mixin split.
_temp_root/get_temp_dir/pick_temp_dir stay in desktop.py: the self-update
flow (api/app_update.py) needs the same temp root, so it isn't zapret-only."""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import CancelledError
from pathlib import Path

import aiohttp
import webview

from .. import integrity
from ..diagnostics import (
    BLOBCAST_TARGETS,
    ECAST_TARGETS,
    build_switch,
    describe_exception,
    render_strategy_results_html,
    render_strategy_results_json,
    run_strategy_suite,
)
from ..paths import resolve_project_path
from ..zapret import update as zapret_update
from ..zapret.process import console_flags, kill_all_winws, stop_windivert_service, wait_for_winws_exit
from ..zapret.strategies import discover_strategies, group_strategies, resolve_strategy
from ..zapret.strategies import save_hostlist as write_hostlist

logger = logging.getLogger(__name__)


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


class ZapretMixin:
    def list_strategies(self) -> dict:
        try:
            strategies = discover_strategies(self._layout().strategies_dir)
            grouped = group_strategies(strategies)
            return {
                "ok": True,
                "error": None,
                "groups": {
                    group: [
                        {
                            "key": s.key,
                            "name": s.filename.removesuffix(".bat"),
                            "aggressive": s.aggressive,
                        }
                        for s in items
                    ]
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
