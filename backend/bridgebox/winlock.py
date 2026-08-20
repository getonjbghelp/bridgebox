"""Retry a file operation past a transient Windows lock.

Shared by zapret/update.py (replacing winws.exe/WinDivert while the driver
unloads asynchronously) and app_update.py (replacing the running exe past an
antivirus scan) - both hit the same two winerrors from unrelated causes and
handle them the same way, so the retry loop itself lives here once.

Attempts/delay are each caller's own tuning, not this module's: how long a
kernel driver takes to unload and how long an antivirus cloud lookup takes
are different budgets, so callers pass their own constants rather than
sharing a default.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# ERROR_ACCESS_DENIED is what a scanner mid-read produces; ERROR_SHARING_VIOLATION
# is the ordinary open-handle case.
LOCKED_WINERRORS = frozenset({5, 32})


def is_locked(exc: BaseException) -> bool:
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) in LOCKED_WINERRORS


def retry_locked(
    op, *, attempts: int, delay_s: float, what: str = "file", sleep=time.sleep
):
    """Run op(), retrying only while Windows says `what` is locked.

    Any other exception raises immediately: a missing source file or a full
    disk will never succeed on retry, and burning the backoff budget to
    rediscover that would just make a real error look like a hang."""
    for attempt in range(1, attempts + 1):
        try:
            return op()
        except Exception as exc:
            if not is_locked(exc) or attempt == attempts:
                raise
            logger.warning(
                "%s is locked (%s), attempt %d/%d - waiting %.1fs",
                what, exc, attempt, attempts, delay_s,
            )
            sleep(delay_s)
