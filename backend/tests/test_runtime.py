from bridgebox.runtime import BridgeRuntime


class FakeCore:
    def __init__(self, *, fail_stop: bool = False):
        self.fail_stop = fail_stop
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self):
        self.start_calls += 1
        return {"running": True}

    async def stop(self):
        self.stop_calls += 1
        if self.fail_stop:
            raise RuntimeError("boom")
        return {"running": False}

    def status(self):
        return {"running": self.start_calls > self.stop_calls}


def test_start_runs_core_coroutine_on_background_loop_and_returns_result():
    core = FakeCore()
    runtime = BridgeRuntime(core)
    try:
        result = runtime.start(timeout=5)
        assert result == {"running": True}
        assert core.start_calls == 1
    finally:
        runtime.shutdown()


def test_stop_runs_core_coroutine_and_returns_result():
    core = FakeCore()
    runtime = BridgeRuntime(core)
    try:
        runtime.start(timeout=5)
        result = runtime.stop(timeout=5)
        assert result == {"running": False}
        assert core.stop_calls == 1
    finally:
        runtime.shutdown()


def test_run_executes_an_arbitrary_coroutine_factory_on_the_background_loop():
    runtime = BridgeRuntime(FakeCore())
    try:
        result = runtime.run(lambda: _double(21), timeout=5)
        assert result == 42
    finally:
        runtime.shutdown()


async def _double(n):
    return n * 2


def test_get_status_reads_core_synchronously_without_loop_round_trip():
    core = FakeCore()
    runtime = BridgeRuntime(core)
    try:
        assert runtime.get_status() == {"running": False}
    finally:
        runtime.shutdown()


def test_shutdown_stops_the_background_thread():
    runtime = BridgeRuntime(FakeCore())
    thread = runtime._thread

    runtime.shutdown()

    thread.join(timeout=2)
    assert thread.is_alive() is False


def test_shutdown_swallows_exceptions_from_core_stop():
    runtime = BridgeRuntime(FakeCore(fail_stop=True))
    runtime.start(timeout=5)

    # must not raise even though core.stop() raises internally
    runtime.shutdown()
