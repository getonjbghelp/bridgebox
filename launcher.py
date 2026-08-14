"""Entry point run.bat launches BridgeBox through.

It exists for one reason: under pythonw.exe there is no stderr, so an error
before logging is configured - a stale venv, a missing dependency, a syntax
error - produced a window that never appeared and not one byte of explanation
anywhere.

run.bat used to buy that explanation with a preflight `python -c "import
bridgebox.desktop"`, which cost a full second and imported the whole
application twice on every single launch. The three obvious cheaper fixes do
not work, and each was measured rather than assumed:

  - `start "" pythonw ... 2>file` writes an EMPTY file. `start` does not pass
    the redirection on to the process it creates.
  - `pythonw ... 2>file` does capture it, but cmd waits for the child (6.2s
    measured against a 6s sleep), so the launcher console would stay open for
    the whole session - the thing `start` was there to prevent.
  - `-m bridgebox.launcher` would not help either: a venv too broken to import
    `bridgebox` cannot report that through a module inside `bridgebox`.

So this is a plain script file, importing nothing but the standard library
until stderr is somewhere it can be read.

Deliberately NOT used by `--console`, where stderr is a real console and
belongs on screen - the redirect below is conditional on there being nothing
there already.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

# Truncated on every launch, so it holds the LAST crash rather than a year of
# them. Everything that happens after logging is configured goes to
# logs/bridgebox.log, which rotates; this file only ever catches what happens
# before that, or what escapes it entirely.
LOG_NAME = "launcher-stderr.log"


def _stderr_sink():
    """Somewhere tracebacks can be read.

    Under pythonw sys.stderr is None and every write to it raises; under
    python.exe it is the console and must be left alone. Returns the stream to
    print a crash to, or None if even opening the file failed - in which case
    the app still starts, which is the right trade for a diagnostic."""
    if sys.stderr is not None:
        return sys.stderr
    try:
        log_dir = Path(__file__).resolve().parent / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        # Line buffered: a crash that kills the process must not leave the
        # explanation sitting in an unflushed buffer.
        handle = open(log_dir / LOG_NAME, "w", encoding="utf-8", buffering=1)
    except OSError:
        return None
    sys.stderr = handle
    return handle


def main() -> None:
    sink = _stderr_sink()
    try:
        from bridgebox.desktop import main as run

        run()
    except SystemExit:
        # main() exits deliberately for an unsupported Windows or a missing
        # frontend build, and both already say why through their own channel.
        raise
    except BaseException:
        # Written to the raw file rather than through sys.stderr: by this point
        # logging_setup.capture_std_streams may have replaced sys.stderr with
        # the logger, whose handlers can already be closing down - and a crash
        # report that depends on the thing that crashed is no report at all.
        if sink is not None:
            traceback.print_exc(file=sink)
            sink.flush()
        raise


if __name__ == "__main__":
    main()
