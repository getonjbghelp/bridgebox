import pytest

from bridgebox import winlock


def _locked_error(winerror: int) -> OSError:
    exc = OSError("locked")
    exc.winerror = winerror
    return exc


def test_retry_locked_retries_a_locked_op_then_succeeds():
    calls = {"n": 0}
    slept = []

    def op():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _locked_error(5)
        return "ok"

    result = winlock.retry_locked(op, attempts=5, delay_s=0.1, sleep=slept.append)

    assert result == "ok"
    assert calls["n"] == 3
    assert slept == [0.1, 0.1]


def test_retry_locked_gives_up_after_the_budget():
    def op():
        raise _locked_error(32)

    with pytest.raises(OSError):
        winlock.retry_locked(op, attempts=3, delay_s=0, sleep=lambda _s: None)


def test_retry_locked_does_not_retry_an_unrelated_error():
    calls = {"n": 0}

    def op():
        calls["n"] += 1
        raise ValueError("not a lock")

    with pytest.raises(ValueError):
        winlock.retry_locked(op, attempts=5, delay_s=0, sleep=lambda _s: None)

    assert calls["n"] == 1
