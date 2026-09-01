from __future__ import annotations

import asyncio
import contextlib
import threading
from concurrent.futures import Future
from typing import Any, Awaitable, Callable, Protocol, TypeVar

T = TypeVar("T")


class RuntimeCoreLike(Protocol):
    async def start(self) -> dict: ...
    async def stop(self) -> dict: ...
    def status(self) -> dict: ...
    def health_status(self) -> dict | None: ...
    def set_zapret_exit_handler(self, handler) -> None: ...


class BridgeRuntime:
    """Thin thread/event-loop shim around RuntimeCore. Owns a background
    thread running its own asyncio loop for the app's lifetime, so pywebview's
    synchronous Api methods can drive RuntimeCore's async start()/stop()
    without blocking pywebview's own (blocking) main GUI loop."""

    def __init__(self, core: RuntimeCoreLike):
        self._core = core
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        # winws dying on its own has to take the rest of the bridge down with
        # it, and only this class knows the loop that teardown must run on.
        # Guarded so a core built by an older test without the hook still works.
        setter = getattr(core, "set_zapret_exit_handler", None)
        if setter is not None:
            setter(self._stop_after_zapret_died)

    def _stop_after_zapret_died(self) -> None:
        """Fire-and-forget teardown, called from the watchdog thread.

        submit() rather than stop(): waiting for the result here would block
        the watchdog thread on a loop that may be busy, for an answer nobody
        reads. The UI learns about it from status()'s zapretNotice."""
        with contextlib.suppress(Exception):
            self.submit(self._core.stop)

    def run(self, coro_factory: Callable[[], Awaitable[T]], timeout: float = 20.0) -> T:
        """Run an arbitrary zero-arg coroutine factory on the background
        loop and return its result - the same mechanism start()/stop() use,
        generalized for diagnostics (test_connection/test_strategies) that
        need to run their own async work on this same loop/thread."""
        return asyncio.run_coroutine_threadsafe(coro_factory(), self._loop).result(timeout)

    def submit(self, coro_factory: Callable[[], Awaitable[T]]) -> "Future[T]":
        """Schedule a coroutine on the background loop and return its Future
        without waiting. For work too long to block a pywebview API call on
        (the strategy suite runs for minutes): the caller polls for progress
        and can cancel(), which raises CancelledError inside the coroutine so
        its cleanup still runs."""
        return asyncio.run_coroutine_threadsafe(coro_factory(), self._loop)

    def start(self, timeout: float = 20.0) -> dict[str, Any]:
        return self.run(self._core.start, timeout)

    def stop(self, timeout: float = 10.0) -> dict[str, Any]:
        return self.run(self._core.stop, timeout)

    def get_status(self) -> dict[str, Any]:
        return self._core.status()

    def get_health_status(self) -> dict[str, Any] | None:
        return self._core.health_status()

    def shutdown(self) -> None:
        """Tear down the bridge and stop the background thread. Never raises
        - the window must be able to close even if teardown partially fails
        (zapret already dead, session already closed, whatever)."""
        with contextlib.suppress(Exception):
            self.stop(timeout=5.0)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)
