"""Log export - one domain slice of Api, mixed into desktop.Api. See
desktop.py's own docstring on why every method returns a plain dict rather
than raising."""
from __future__ import annotations

import logging
from pathlib import Path

import webview

from ..diagnostics import describe_exception
from ..log_buffer import EXPORT_FORMATS, render_log

logger = logging.getLogger(__name__)


class LogsMixin:
    def get_log_lines(self, since_seq: int = 0, limit: int = 500) -> dict:
        try:
            result = self._log_buffer.since(since_seq, limit)
            return {"ok": True, "error": None, **result}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "lines": [], "nextSeq": since_seq}

    def export_logs(self, fmt: str) -> dict:
        """Save the whole buffer through the native save dialog.

        Reads the buffer rather than taking the frontend's filtered copy: an
        export is for a bug report, and a report missing whatever the level
        pills happened to be hiding is worse than useless. Same dialog shape as
        export_strategy_results - one pattern for every file this app writes."""
        try:
            if fmt not in EXPORT_FORMATS:
                return {"ok": False, "error": f"неизвестный формат: {fmt!r}", "path": ""}
            if self._window is None:
                return {"ok": False, "error": "окно недоступно", "path": ""}

            lines = self._log_buffer.snapshot()
            if not lines:
                return {"ok": False, "error": "лог пуст", "path": ""}

            file_types = {
                "log": ("Журнал (*.log)",),
                "json": ("JSON (*.json)",),
                "html": ("HTML (*.html)",),
            }[fmt]
            chosen = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=f"bridgebox-logs.{fmt}",
                file_types=file_types,
            )
            if not chosen:
                return {"ok": True, "error": None, "path": ""}  # cancelled
            path = Path(chosen[0] if isinstance(chosen, (list, tuple)) else str(chosen))
            path.write_text(render_log(lines, fmt), encoding="utf-8")
            logger.info("exported %d log lines (%s) to %s", len(lines), fmt, path)
            return {"ok": True, "error": None, "path": str(path)}
        except Exception as exc:
            logger.exception("log export failed")
            return {"ok": False, "error": describe_exception(exc), "path": ""}
