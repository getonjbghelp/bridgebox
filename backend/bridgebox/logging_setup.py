from __future__ import annotations

import json
import logging
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from .config import LoggingConfig

LOGGER_NAME = "bridgebox"

# Third-party loggers worth surfacing in the app's own log. TLS handshake
# failures - the game refusing our local certificate - are raised deep inside
# aiohttp/asyncio and would otherwise vanish silently, leaving "the game just
# won't connect" with nothing in the log to explain it.
# "aiohttp.access" is the per-request access log for our own HTTPS server.
# It is on by default in aiohttp but writes to a logger nothing was attached
# to, so every request the game made to the bridge went unrecorded.
ATTACHED_LOGGERS = (
    "aiohttp.server",
    "aiohttp.web",
    "aiohttp.client",
    "aiohttp.access",
    "asyncio",
)

# Where each record came from. Without module.function:line a log line tells
# you what happened but not which code to go change, which is most of the
# value when adapting a function to a protocol that keeps surprising us.
FILE_FORMAT = (
    "%(asctime)s %(levelname)-7s %(name)s "
    "%(module)s.%(funcName)s:%(lineno)d [%(threadName)s] %(message)s"
)
CONSOLE_FORMAT = "%(levelname)-7s %(module)s.%(funcName)s:%(lineno)d %(message)s"

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


class UiLogHandler(logging.Handler):
    """Formats each record as a single JSON line and pushes it to `sink` -
    the Logs screen consumes this via a WS/polling channel from pywebview."""

    def __init__(self, sink: Callable[[str], None]):
        super().__init__()
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload = {
                "time": record.created,
                "level": record.levelname.lower(),
                "logger": record.name,
                "message": record.getMessage(),
                # Call site, so a line in the Logs screen points at the code
                # that produced it instead of just describing a symptom.
                "module": record.module,
                "func": record.funcName,
                "line": record.lineno,
                "thread": record.threadName,
            }
            if record.exc_info:
                # logger.exception() used to reach the UI as its one-line
                # message with the traceback silently dropped - the stack is
                # the whole point of logging an exception.
                payload["traceback"] = "".join(traceback.format_exception(*record.exc_info))
            elif record.exc_text:
                payload["traceback"] = record.exc_text
            self._sink(json.dumps(payload, ensure_ascii=False))
        except Exception:
            # A failing sink must never take the application down with it.
            self.handleError(record)


class _StreamToLogger:
    """A file-like object that turns writes into log records.

    Installed over sys.stdout/sys.stderr so that print(), a library that
    writes to stderr directly, and an unhandled traceback all reach the Logs
    screen instead of a console the user does not have. Under pythonw.exe -
    how BridgeBox actually launches - those two streams are None, and anything
    written to them is not merely invisible, it raises.

    Line-buffered because a logger record is a line: writes arrive in
    fragments (print() alone sends the text and the newline separately) and one
    record per fragment would shred every message."""

    def __init__(self, log: logging.Logger, level: int, name: str):
        self._log = log
        self._level = level
        self._buffer = ""
        # Re-entry guard, per thread. The logging module reports a failing
        # handler by writing to sys.stderr - which, once captured, is this
        # object, which logs, which fails again: a RecursionError instead of
        # the one broken handler it was trying to tell you about. Caught by a
        # test rather than reasoned about.
        self._writing = threading.local()
        # Enough of a real stream for the things that probe one.
        self.name = name
        self.encoding = "utf-8"
        self.errors = "replace"

    def _emit(self, line: str) -> None:
        if getattr(self._writing, "busy", False):
            # Already inside a write on this thread: send it somewhere that
            # cannot come back here, or nowhere. Losing a line beats losing
            # the process.
            fallback = sys.__stderr__
            if fallback is not None:
                fallback.write(line + "\n")
            return
        self._writing.busy = True
        try:
            self._log.log(self._level, "%s", line)
        finally:
            self._writing.busy = False

    def write(self, text) -> int:
        text = str(text)
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._emit(line.rstrip())
        return len(text)

    def flush(self) -> None:
        line, self._buffer = self._buffer, ""
        if line.strip():
            self._emit(line.rstrip())

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def fileno(self) -> int:
        # Anything asking for a real file descriptor - subprocess inheriting
        # this stream, for instance - has to be told there is none, or it
        # silently gets the wrong one.
        raise OSError("this stream is a logger, not a file")


def capture_std_streams(logger: logging.Logger | None = None) -> None:
    """Route sys.stdout/sys.stderr into the app log.

    Separate from setup_logging and opt-in, because a test that calls
    setup_logging must not lose its own output. main() is the only caller.

    Deliberately after the console handler is built: that handler writes to
    sys.__stderr__, the ORIGINAL stream, so redirecting sys.stderr here cannot
    feed the logger back into itself."""
    log = logger if logger is not None else logging.getLogger(LOGGER_NAME)
    sys.stdout = _StreamToLogger(log, logging.INFO, "<stdout>")
    sys.stderr = _StreamToLogger(log, logging.ERROR, "<stderr>")


def setup_logging(
    config: LoggingConfig, *, ui_sink: Callable[[str], None] | None = None
) -> logging.Logger:
    """Configure the "bridgebox" logger: rotating file handler at the
    configured level/dir/size, plus an optional UI JSON-line handler. Safe to
    call repeatedly - replaces handlers rather than stacking duplicates.

    config.level gates the *file and console* handlers, not the logger: the
    Logs screen does its own level filtering, so its DEBUG pill can only ever
    match if DEBUG records reach the buffer. Setting the level on the logger
    dropped them before any handler ran, which meant the pill showed nothing
    at the default level=info no matter what the app did. Keeping the logger
    itself open and filtering per handler also means changing the level in
    Settings affects the file immediately, with no live-reconfigure path to
    build."""
    logger = logging.getLogger(LOGGER_NAME)
    level = _LEVELS[config.level]
    logger.setLevel(logging.DEBUG)

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    log_dir = Path(config.dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        log_dir / "bridgebox.log",
        maxBytes=config.rotate_mb * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
    file_handler.setLevel(level)
    logger.addHandler(file_handler)

    # `run.bat --console` leaves a console open; without this it stays blank
    # while the log file fills up, so anything diagnosed live has to be tailed
    # by hand.
    #
    # sys.__stderr__ rather than sys.stderr, and skipped entirely when it is
    # None. Both matter: under pythonw.exe there is no stderr at all, and once
    # capture_std_streams() has redirected sys.stderr INTO this logger, reading
    # it here on a second setup_logging call would close the loop and recurse.
    if sys.__stderr__ is not None:
        console_handler = logging.StreamHandler(stream=sys.__stderr__)
        console_handler.setFormatter(logging.Formatter(CONSOLE_FORMAT))
        console_handler.setLevel(level)
        logger.addHandler(console_handler)

    if ui_sink is not None:
        # Deliberately unfiltered - the Logs screen's own pills are the filter.
        logger.addHandler(UiLogHandler(ui_sink))

    logger.propagate = False

    for name in ATTACHED_LOGGERS:
        attached = logging.getLogger(name)
        for handler in list(attached.handlers):
            if isinstance(handler, (RotatingFileHandler, UiLogHandler, logging.StreamHandler)):
                attached.removeHandler(handler)
        for handler in logger.handlers:
            attached.addHandler(handler)
        # These keep the gate on the logger, unlike bridgebox's own above: at
        # level=debug the point is to see aiohttp's request/connection detail
        # (a fixed WARNING floor threw that away), but letting it through
        # unconditionally would churn the UI's bounded deque with third-party
        # noise the Logs screen has no pill for.
        attached.setLevel(level)

    logger.debug(
        "logging configured: level=%s dir=%s rotate=%dMB handlers=%d third_party=%s",
        config.level,
        log_dir,
        config.rotate_mb,
        len(logger.handlers),
        ",".join(ATTACHED_LOGGERS),
    )
    return logger
