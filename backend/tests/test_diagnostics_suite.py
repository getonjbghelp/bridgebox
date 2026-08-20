from pathlib import Path

from bridgebox.diagnostics import ECAST_TARGETS, probe_targets, run_strategy_suite
from bridgebox.zapret.strategies import discover_strategies


class FakeResponse:
    def __init__(self, status=200):
        self.status = status
        # Mirrors aiohttp: probe logging reads Content-Type off the response.
        self.headers = {"Content-Type": "application/json"}

    async def read(self):
        return b""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Round-robins canned outcomes per URL so different targets can behave
    differently (e.g. one blocked, one reachable) within a single probe."""

    def __init__(self, outcomes: dict[str, Exception | int] | None = None):
        self.outcomes = outcomes or {}
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        outcome = self.outcomes.get(url, 200)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(status=outcome)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_probe_targets_reports_each_target_independently():
    session = FakeSession()

    results = await probe_targets(session)

    assert set(results.keys()) == {name for name, _ in ECAST_TARGETS}
    assert all(r["ok"] for r in results.values())
    assert all(isinstance(r["elapsedMs"], float) for r in results.values())


async def test_probe_targets_survives_one_target_failing():
    failing_url = ECAST_TARGETS[0][1]
    session = FakeSession(outcomes={failing_url: ConnectionError("blocked")})

    results = await probe_targets(session)

    failing_name = ECAST_TARGETS[0][0]
    other_name = ECAST_TARGETS[1][0]
    assert results[failing_name]["ok"] is False
    assert "blocked" in results[failing_name]["error"]
    assert results[other_name]["ok"] is True


async def test_run_strategy_suite_switches_and_probes_each_strategy(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "General.bat").write_text("@echo off\n")
    (strategies_dir / "Alternative 1.bat").write_text("@echo off\n")
    strategies = discover_strategies(strategies_dir)

    switched = []

    async def fake_switch(key):
        switched.append(key)

    def fake_session_factory():
        return FakeSession()

    results = await run_strategy_suite(
        strategies.values(),
        switch=fake_switch,
        session_factory=fake_session_factory,
        settle_s=0,
    )

    # discover_strategies() sorts alphabetically by filename, not by group -
    # grouping/ordering for display is group_strategies()'s job, not this one's.
    assert set(switched) == {"general", "alternative-1"}
    assert {r["key"] for r in results} == {"general", "alternative-1"}
    assert all(r["ok"] for r in results)
    assert all(set(r["targets"].keys()) == {name for name, _ in ECAST_TARGETS} for r in results)


async def test_run_strategy_suite_streams_each_result_as_it_lands(tmp_path: Path):
    """The popup fills in as the suite runs; without streaming a user stares
    at an empty table for the minutes a full run takes."""
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "General.bat").write_text("@echo off\n")
    (strategies_dir / "Alternative 1.bat").write_text("@echo off\n")
    strategies = discover_strategies(strategies_dir)

    streamed: list[dict] = []
    seen_lengths: list[int] = []

    async def fake_switch(key):
        # Every switch should see every result recorded before it.
        seen_lengths.append(len(streamed))

    results = await run_strategy_suite(
        strategies.values(),
        switch=fake_switch,
        session_factory=lambda: FakeSession(),
        settle_s=0,
        on_result=streamed.append,
    )

    assert seen_lengths == [0, 1]  # streamed incrementally, not all at the end
    assert [r["key"] for r in streamed] == [r["key"] for r in results]


async def test_probe_targets_reports_a_timeout_with_a_readable_error():
    """asyncio.TimeoutError stringifies to '', which used to surface in the
    UI as a blank error cell with no hint that anything timed out."""
    import asyncio

    failing_url = ECAST_TARGETS[0][1]
    session = FakeSession(outcomes={failing_url: asyncio.TimeoutError()})

    results = await probe_targets(session)

    error = results[ECAST_TARGETS[0][0]]["error"]
    assert error, "timeout must not produce an empty error string"
    assert "TimeoutError" in error


async def test_run_strategy_suite_switch_failure_is_recorded_not_raised(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "General.bat").write_text("@echo off\n")
    strategies = discover_strategies(strategies_dir)

    async def failing_switch(key):
        raise RuntimeError("zapret refused to start")

    results = await run_strategy_suite(
        strategies.values(),
        switch=failing_switch,
        session_factory=lambda: FakeSession(),
        settle_s=0,
    )

    assert results[0]["ok"] is False
    assert "zapret refused to start" in results[0]["error"]


# ---- Blobcast pings ------------------------------------------------------


def test_blobcast_targets_are_distinct_hosts_from_ecast():
    """The two lists must not silently overlap - a shared host would make
    "Ecast" and "Blobcast" test the same thing under different labels."""
    from bridgebox.diagnostics import BLOBCAST_TARGETS

    ecast_names = {name for name, _ in ECAST_TARGETS}
    blobcast_names = {name for name, _ in BLOBCAST_TARGETS}
    assert not (ecast_names & blobcast_names)
    assert blobcast_names == {"blobcast.jackboxgames.com"}


async def test_probe_targets_can_be_pointed_at_blobcast_targets():
    """probe_targets takes any (name, url) list - it isn't hardcoded to
    ECAST_TARGETS, which is what lets the strategy suite run a Blobcast pass
    without a second copy of this function."""
    from bridgebox.diagnostics import BLOBCAST_TARGETS

    session = FakeSession()

    results = await probe_targets(session, BLOBCAST_TARGETS)

    assert set(results.keys()) == {name for name, _ in BLOBCAST_TARGETS}
    assert all(r["ok"] for r in results.values())


async def test_run_strategy_suite_accepts_an_explicit_target_list(tmp_path: Path):
    """The two-stage "both" mode in desktop.Api runs this twice with two
    different target lists - it has to actually honour the targets kwarg
    rather than always falling back to the ECAST_TARGETS default."""
    from bridgebox.diagnostics import BLOBCAST_TARGETS

    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "General.bat").write_text("@echo off\n")
    strategies = discover_strategies(strategies_dir)

    async def fake_switch(key):
        pass

    results = await run_strategy_suite(
        strategies.values(),
        switch=fake_switch,
        session_factory=lambda: FakeSession(),
        targets=BLOBCAST_TARGETS,
        settle_s=0,
    )

    assert set(results[0]["targets"].keys()) == {name for name, _ in BLOBCAST_TARGETS}


# ---- export rendering -----------------------------------------------------


def _sample_results():
    return [
        {
            "key": "general",
            "name": "General",
            "ok": True,
            "targetSet": "ecast",
            "targets": {
                "ecast.jackboxgames.com": {"ok": True, "elapsedMs": 123.4, "status": 200, "error": None},
                "ecast-prod-use2.jackboxgames.com": {
                    "ok": False, "elapsedMs": None, "status": None, "error": "timed out",
                },
            },
            "error": None,
        },
        {
            "key": "alternative-1",
            "name": "Alternative 1",
            "ok": False,
            "targetSet": "blobcast",
            "targets": {},
            "error": "все цели недоступны",
        },
    ]


def test_render_strategy_results_json_round_trips_through_json():
    import json

    from bridgebox.diagnostics import render_strategy_results_json

    text = render_strategy_results_json(_sample_results())
    payload = json.loads(text)

    assert payload["format"] == "bridgebox-strategy-test"
    assert [r["key"] for r in payload["results"]] == ["general", "alternative-1"]
    assert payload["results"][0]["targets"]["ecast.jackboxgames.com"]["elapsedMs"] == 123.4


def test_render_strategy_results_html_is_well_formed_and_escapes_content():
    from bridgebox.diagnostics import render_strategy_results_html

    html = render_strategy_results_html(
        [
            {
                "key": "k",
                "name": "<script>alert(1)</script>",
                "ok": False,
                "targetSet": "ecast",
                "targets": {},
                "error": "boom & <bad>",
            }
        ]
    )

    assert html.startswith("<!doctype html>")
    assert "<script>alert(1)</script>" not in html  # escaped, not injected raw
    assert "&lt;script&gt;" in html
    assert "boom &amp; &lt;bad&gt;" in html


def test_render_strategy_results_html_handles_an_empty_run():
    """Exporting a cancelled-before-anything-landed run must not raise."""
    from bridgebox.diagnostics import render_strategy_results_html

    html = render_strategy_results_html([])

    assert "<table>" in html
