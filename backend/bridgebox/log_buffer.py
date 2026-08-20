from __future__ import annotations

import html
import json
import threading
from collections import deque
from datetime import datetime


class LogBuffer:
    """In-memory ring buffer feeding the Logs screen. Wired as the ui_sink
    for logging_setup.setup_logging() - each append() call receives one
    JSON line exactly as UiLogHandler produces it. The Logs screen polls
    since(seq) roughly once a second rather than a push channel (see PRD:
    push via pywebview's evaluate_js would need the logger's call site,
    including the event-loop thread, to hold a Window reference - not worth
    the extra cross-thread hazard for a 1s-granularity debug log view)."""

    # Sized for level=debug, where a single bridge start plus a few proxied
    # requests can run to hundreds of lines.
    def __init__(self, maxlen: int = 5000):
        self._lines: deque[dict] = deque(maxlen=maxlen)
        self._next_seq = 0
        # append() runs on whichever thread called the logger - the asyncio
        # loop, the GUI thread, the taskkill thread - while since() runs on
        # the pywebview API thread. Unguarded, iterating the deque in since()
        # raised "deque mutated during iteration" straight out of
        # get_log_lines (worst exactly at level=debug, where every WS frame
        # is logged), and the non-atomic += handed out duplicate seq numbers,
        # which made the frontend's since(nextSeq) paging skip lines.
        self._lock = threading.Lock()

    def append(self, json_line: str) -> None:
        payload = json.loads(json_line)
        with self._lock:
            payload["seq"] = self._next_seq
            self._next_seq += 1
            self._lines.append(payload)

    def since(self, since_seq: int = 0, limit: int = 500) -> dict:
        with self._lock:
            # Snapshot under the lock, filter outside it: the copy is the only
            # part that must not race an append, and holding the lock across
            # the filter would put it in the path of every log call.
            lines = list(self._lines)
        page = [line for line in lines if line["seq"] >= since_seq][:limit]
        next_seq = page[-1]["seq"] + 1 if page else since_seq
        return {"lines": page, "nextSeq": next_seq}

    def snapshot(self) -> list[dict]:
        """Everything currently held, for export. A copy, so the caller can
        take its time rendering without blocking every log call."""
        with self._lock:
            return list(self._lines)


# The three export formats the Logs screen offers, and what each is for:
# .log for pasting into a chat, .json for a script, .html for opening.
EXPORT_FORMATS = ("log", "json", "html")


def _stamp(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "?"


def _origin(line: dict) -> str:
    module = line.get("module")
    if not module:
        return ""
    return f" ({module}.{line.get('func')}:{line.get('line')})"


def render_log_text(lines: list[dict]) -> str:
    """The same shape the file log already has, so a pasted export and a
    pasted logs/bridgebox.log line read identically."""
    out = []
    for line in lines:
        out.append(
            f"{_stamp(line.get('time', 0))} "
            f"{str(line.get('level', '')).upper():<7} "
            f"{line.get('logger', '')}{_origin(line)} {line.get('message', '')}"
        )
        traceback = line.get("traceback")
        if traceback:
            out.append(str(traceback).rstrip())
    return "\n".join(out) + "\n"


def render_log_json(lines: list[dict]) -> str:
    return json.dumps(lines, ensure_ascii=False, indent=2)


def _escape(text: str) -> str:
    return html.escape(text, quote=True)


def render_log_html(lines: list[dict]) -> str:
    """Self-contained, openable by double-clicking - the same constraint every
    other exported artifact in this app meets (profiles, strategy results)."""
    rows = []
    for line in lines:
        level = _escape(str(line.get("level", "info")))
        traceback = line.get("traceback")
        trace_html = (
            f"<pre>{_escape(str(traceback).rstrip())}</pre>" if traceback else ""
        )
        rows.append(
            f'<tr class="{level}">'
            f'<td class="t">{_escape(_stamp(line.get("time", 0)))}</td>'
            f'<td class="l">{level.upper()}</td>'
            f'<td>{_escape(str(line.get("message", "")))}'
            f'<span class="o">{_escape(_origin(line).strip())}</span>{trace_html}</td>'
            "</tr>"
        )
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        "<!doctype html>\n"
        '<html lang="ru"><head><meta charset="utf-8">\n'
        "<title>BridgeBox — логи</title>\n"
        "<style>\n"
        '  body { font: 13px/1.5 -apple-system, "Segoe UI", system-ui, sans-serif;'
        " margin: 24px; color: #0b1220; }\n"
        "  h1 { font-size: 18px; }\n"
        "  table { border-collapse: collapse; width: 100%; }\n"
        "  td { border-bottom: 1px solid #e5e7eb; padding: 4px 8px; vertical-align: top; }\n"
        '  td.t, td.l { white-space: nowrap; font-family: ui-monospace, Consolas, monospace; }\n'
        "  .o { color: #6b7280; margin-left: 8px; font-size: 11px; }\n"
        "  pre { margin: 4px 0 0; white-space: pre-wrap; font-size: 11px; color: #7f1d1d; }\n"
        "  tr.error td.l { color: #b91c1c; }\n"
        "  tr.warning td.l { color: #b45309; }\n"
        "  tr.debug td { color: #6b7280; }\n"
        "</style></head><body>\n"
        f"<h1>BridgeBox — логи ({len(lines)} строк, {_escape(generated)})</h1>\n"
        "<table>" + "\n".join(rows) + "</table>\n"
        "</body></html>\n"
    )


def render_log(lines: list[dict], fmt: str) -> str:
    if fmt == "log":
        return render_log_text(lines)
    if fmt == "json":
        return render_log_json(lines)
    if fmt == "html":
        return render_log_html(lines)
    raise ValueError(f"неизвестный формат: {fmt!r}")
