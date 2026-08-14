import json
import logging
import sys
from pathlib import Path

from bridgebox.config import LoggingConfig
from bridgebox.logging_setup import _StreamToLogger, setup_logging


def test_setup_logging_creates_log_dir_and_writes_file(tmp_path: Path):
    log_dir = tmp_path / "logs"
    config = LoggingConfig(level="info", dir=str(log_dir), rotate_mb=5)

    logger = setup_logging(config)
    logger.info("hello from bridgebox")
    for handler in logger.handlers:
        handler.flush()

    log_file = log_dir / "bridgebox.log"
    assert log_file.exists()
    assert "hello from bridgebox" in log_file.read_text(encoding="utf-8")


def test_setup_logging_respects_level_filtering(tmp_path: Path):
    log_dir = tmp_path / "logs"
    config = LoggingConfig(level="warning", dir=str(log_dir), rotate_mb=5)

    logger = setup_logging(config)
    logger.info("should be filtered out")
    logger.warning("should appear")
    for handler in logger.handlers:
        handler.flush()

    content = (log_dir / "bridgebox.log").read_text(encoding="utf-8")
    assert "should be filtered out" not in content
    assert "should appear" in content


def test_ui_sink_gets_every_level_regardless_of_the_configured_one(tmp_path: Path):
    """The Logs screen filters by level itself, so its DEBUG pill can only
    work if DEBUG records actually reach the buffer. Setting the level on the
    logger (rather than on the file/console handlers) killed them before any
    handler ran, which made the pill permanently show nothing at the default
    level=info."""
    log_dir = tmp_path / "logs"
    config = LoggingConfig(level="info", dir=str(log_dir), rotate_mb=5)
    received: list[str] = []

    logger = setup_logging(config, ui_sink=received.append)
    logger.debug("debug detail")
    logger.info("info line")
    for handler in logger.handlers:
        handler.flush()

    levels = {json.loads(line)["level"] for line in received}
    assert {"debug", "info"} <= levels

    # ...and the file still honours the configured level, or level=info would
    # silently start writing a debug firehose to disk.
    content = (log_dir / "bridgebox.log").read_text(encoding="utf-8")
    assert "debug detail" not in content
    assert "info line" in content


def test_setup_logging_configures_rotation_size(tmp_path: Path):
    log_dir = tmp_path / "logs"
    config = LoggingConfig(level="info", dir=str(log_dir), rotate_mb=2)

    logger = setup_logging(config)

    file_handlers = [h for h in logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(file_handlers) == 1
    assert file_handlers[0].maxBytes == 2 * 1024 * 1024


def test_setup_logging_is_idempotent_no_duplicate_handlers(tmp_path: Path):
    log_dir = tmp_path / "logs"
    config = LoggingConfig(level="info", dir=str(log_dir), rotate_mb=5)

    setup_logging(config)
    logger = setup_logging(config)
    logger.info("only once")
    for handler in logger.handlers:
        handler.flush()

    content = (log_dir / "bridgebox.log").read_text(encoding="utf-8")
    assert content.count("only once") == 1


def test_aiohttp_errors_reach_the_ui_sink(tmp_path: Path):
    """A TLS handshake failure (game rejecting our cert) surfaces through
    aiohttp's own logger - it has to reach the Logs screen or the user sees
    "doesn't connect" with an empty log."""
    log_dir = tmp_path / "logs"
    config = LoggingConfig(level="debug", dir=str(log_dir), rotate_mb=5)
    received: list[str] = []

    setup_logging(config, ui_sink=received.append)
    logging.getLogger("aiohttp.server").error("SSL handshake failed")

    assert any("SSL handshake failed" in line for line in received)


def test_ui_sink_receives_json_lines(tmp_path: Path):
    log_dir = tmp_path / "logs"
    config = LoggingConfig(level="debug", dir=str(log_dir), rotate_mb=5)
    received: list[str] = []

    logger = setup_logging(config, ui_sink=received.append)
    logger.error("boom")

    payloads = [json.loads(line) for line in received]
    payload = next(p for p in payloads if p["message"] == "boom")
    assert payload["level"] == "error"
    assert payload["logger"] == "bridgebox"
    # Call site travels with the record so a Logs line points at real code.
    assert payload["module"] == "test_logging_setup"
    assert payload["func"] == "test_ui_sink_receives_json_lines"
    assert isinstance(payload["line"], int)


def test_ui_sink_carries_the_traceback_for_logged_exceptions(tmp_path: Path):
    """logger.exception() reached the Logs screen as a bare one-line message
    with the stack dropped, which is the part worth reading."""
    log_dir = tmp_path / "logs"
    config = LoggingConfig(level="debug", dir=str(log_dir), rotate_mb=5)
    received: list[str] = []

    logger = setup_logging(config, ui_sink=received.append)
    try:
        raise ValueError("inner cause")
    except ValueError:
        logger.exception("outer failure")

    payload = next(
        json.loads(line) for line in received if json.loads(line)["message"] == "outer failure"
    )
    assert "ValueError: inner cause" in payload["traceback"]
    assert "raise ValueError" in payload["traceback"]


def test_a_failing_ui_sink_does_not_break_logging(tmp_path: Path):
    """The Logs screen is a debug aid; it must not be able to take the app
    down by raising back into the logging call."""
    log_dir = tmp_path / "logs"
    config = LoggingConfig(level="info", dir=str(log_dir), rotate_mb=5)

    def exploding_sink(_line: str) -> None:
        raise RuntimeError("sink is broken")

    logger = setup_logging(config, ui_sink=exploding_sink)
    logger.info("must still reach the file")  # must not raise
    for handler in logger.handlers:
        handler.flush()

    assert "must still reach the file" in (log_dir / "bridgebox.log").read_text(encoding="utf-8")


# ---- capturing stdout/stderr ----


def test_print_reaches_the_log(tmp_path, caplog):
    """Under pythonw sys.stdout is None, so a stray print was not merely
    invisible - it raised. Everything the app writes has to land in one place."""
    from bridgebox.logging_setup import capture_std_streams

    original = sys.stdout, sys.stderr
    try:
        capture_std_streams(logging.getLogger("bridgebox"))
        with caplog.at_level(logging.INFO, logger="bridgebox"):
            print("hello from a library")
            print("boom", file=sys.stderr)
    finally:
        sys.stdout, sys.stderr = original

    assert "hello from a library" in caplog.text
    assert "boom" in caplog.text


def test_a_captured_stderr_cannot_feed_the_console_handler_back_into_itself(tmp_path):
    """The console handler writes to stderr. If it read the CAPTURED stderr,
    one log record would produce another, forever."""
    from bridgebox.logging_setup import capture_std_streams, setup_logging

    original = sys.stdout, sys.stderr
    try:
        config = LoggingConfig(level="debug", dir=str(tmp_path))
        logger = setup_logging(config)
        capture_std_streams(logger)
        # The second configure is the dangerous one: it runs with stderr
        # already redirected into this very logger.
        logger = setup_logging(config)
        logger.info("still fine")
    finally:
        sys.stdout, sys.stderr = original

    streams = [
        getattr(handler, "stream", None)
        for handler in logger.handlers
        if isinstance(handler, logging.StreamHandler)
    ]
    assert not any(isinstance(stream, _StreamToLogger) for stream in streams)


def test_writes_are_split_into_lines_not_fragments(caplog):
    """print() sends the text and the newline as two separate writes; one
    record per write would shred every message in the log."""
    log = logging.getLogger("bridgebox.test.stream")
    stream = _StreamToLogger(log, logging.INFO, "<test>")

    with caplog.at_level(logging.INFO, logger="bridgebox.test.stream"):
        stream.write("first ")
        stream.write("line\nsecond line\n")

    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["first line", "second line"]


def test_a_broken_handler_cannot_recurse_through_the_captured_stderr():
    """logging reports a failing handler by writing to sys.stderr. Once that
    IS the logger, one broken handler becomes a RecursionError - which is what
    you get told about instead of the actual problem."""
    log = logging.getLogger("bridgebox.test.recursion")
    log.handlers = []
    log.propagate = False

    class Exploding(logging.Handler):
        def emit(self, record):
            # What a real handler does with a failure: hand it to handleError,
            # which prints the traceback to sys.stderr - the captured one.
            try:
                raise RuntimeError("this handler is broken")
            except RuntimeError:
                self.handleError(record)

    log.addHandler(Exploding())
    stream = _StreamToLogger(log, logging.ERROR, "<stderr>")

    original = sys.stderr
    sys.stderr = stream
    try:
        stream.write("something went wrong\n")  # must return, not recurse
    finally:
        sys.stderr = original
        log.handlers = []
