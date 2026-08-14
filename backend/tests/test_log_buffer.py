import json

import pytest

from bridgebox.log_buffer import EXPORT_FORMATS, LogBuffer, render_log


def _line(level: str, message: str) -> str:
    return json.dumps({"time": 1.0, "level": level, "logger": "bridgebox", "message": message})


def test_append_and_since_returns_all_lines_from_zero():
    buffer = LogBuffer()
    buffer.append(_line("info", "first"))
    buffer.append(_line("warning", "second"))

    result = buffer.since(0)

    assert [line["message"] for line in result["lines"]] == ["first", "second"]
    assert result["nextSeq"] == 2


def test_since_seq_filters_out_already_seen_lines():
    buffer = LogBuffer()
    buffer.append(_line("info", "first"))
    first_page = buffer.since(0)
    buffer.append(_line("info", "second"))

    result = buffer.since(first_page["nextSeq"])

    assert [line["message"] for line in result["lines"]] == ["second"]


def test_since_with_no_new_lines_returns_empty_and_same_next_seq():
    buffer = LogBuffer()
    buffer.append(_line("info", "first"))
    first_page = buffer.since(0)

    result = buffer.since(first_page["nextSeq"])

    assert result["lines"] == []
    assert result["nextSeq"] == first_page["nextSeq"]


def test_limit_caps_page_size():
    buffer = LogBuffer()
    for i in range(5):
        buffer.append(_line("info", f"line-{i}"))

    result = buffer.since(0, limit=2)

    assert [line["message"] for line in result["lines"]] == ["line-0", "line-1"]
    assert result["nextSeq"] == 2


def test_maxlen_evicts_oldest_lines():
    buffer = LogBuffer(maxlen=3)
    for i in range(5):
        buffer.append(_line("info", f"line-{i}"))

    result = buffer.since(0)

    assert [line["message"] for line in result["lines"]] == ["line-2", "line-3", "line-4"]


def test_each_line_gets_an_increasing_seq():
    buffer = LogBuffer()
    buffer.append(_line("info", "a"))
    buffer.append(_line("info", "b"))

    result = buffer.since(0)

    assert [line["seq"] for line in result["lines"]] == [0, 1]


def test_concurrent_appends_and_reads_stay_consistent():
    """append() runs on whichever thread called the logger - the asyncio loop,
    the GUI thread, the taskkill thread - while since() runs on the pywebview
    API thread. Neither the deque iteration nor the seq counter was guarded,
    which raised "deque mutated during iteration" out of get_log_lines and
    handed out duplicate seq numbers (so the Logs screen dropped lines).

    Probabilistic by nature, but the unguarded version fails this within
    milliseconds - measured at ~20 RuntimeErrors in two seconds."""
    import threading

    buffer = LogBuffer(maxlen=200)  # small, so append() also evicts from the left
    errors: list[Exception] = []
    seen: list[int] = []
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            try:
                buffer.append(_line("info", "m"))
            except Exception as exc:  # noqa: BLE001 - collected, asserted below
                errors.append(exc)

    def reader():
        while not stop.is_set():
            try:
                page = [line["seq"] for line in buffer.since(0, 500)["lines"]]
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                continue
            # Checked per page, not across polls: a reader polling since(0) in
            # a loop legitimately sees the same seq again every time. What must
            # never happen is one page carrying a seq twice - that means two
            # appends collided on the counter and one line became unreachable.
            seen.append(len(page) - len(set(page)))

    threads = [threading.Thread(target=writer) for _ in range(3)]
    threads += [threading.Thread(target=reader) for _ in range(2)]
    for thread in threads:
        thread.start()
    stop.wait(0.5)
    stop.set()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sum(seen) == 0, f"{sum(seen)} duplicate seq numbers within a single page"


# ---- export ----


def _sample() -> list[dict]:
    return [
        {
            "seq": 0,
            "time": 1_700_000_000.0,
            "level": "info",
            "logger": "bridgebox",
            "message": "мост запущен",
            "module": "runtime_core",
            "func": "start",
            "line": 42,
        },
        {
            "seq": 1,
            "time": 1_700_000_001.0,
            "level": "error",
            "logger": "bridgebox",
            "message": "boom <script>",
            "traceback": "Traceback...\nValueError: boom",
        },
    ]


def test_every_export_format_carries_the_traceback():
    """The stack is the reason an export exists - a bug report without it is a
    sentence describing a crash."""
    for fmt in EXPORT_FORMATS:
        rendered = render_log(_sample(), fmt)
        assert "ValueError: boom" in rendered, fmt


def test_the_html_export_escapes_log_content():
    """Log lines carry whatever came off the wire. An export opened in a
    browser must not execute it."""
    rendered = render_log(_sample(), "html")

    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered


def test_the_json_export_round_trips():
    restored = json.loads(render_log(_sample(), "json"))

    assert [line["seq"] for line in restored] == [0, 1]


def test_an_unknown_format_is_refused():
    with pytest.raises(ValueError):
        render_log(_sample(), "pdf")


def test_snapshot_is_a_copy_not_the_live_deque():
    """The caller renders at its own pace while logging continues; handing out
    the deque itself is the "mutated during iteration" bug all over again."""
    buffer = LogBuffer()
    buffer.append(json.dumps({"level": "info", "message": "one"}))
    taken = buffer.snapshot()
    buffer.append(json.dumps({"level": "info", "message": "two"}))

    assert len(taken) == 1
