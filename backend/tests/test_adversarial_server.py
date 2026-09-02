"""Regression tests for a trust-boundary defect in the bridge server, now
fixed: bridgebox.server.blobcast.is_plain_hostname() - whose docstring is
literally "Whether `value` is a bare hostname safe to interpolate into a
URL" - used to accept a value with a trailing newline. _HOSTNAME_RE was
applied with re.match() and ended in `$`, and in Python `$` also matches just
before a final newline, so "host\n" passed the "safe for a URL" gate. Fixed
by anchoring on \\Z instead, which only matches the true end of the string.

is_plain_hostname is the ONLY check standing between the upstream's /room
response `server` field (network-controlled) and the string that
BlobcastSessions.remember() stores and later interpolates into the socket.io
upstream URL. A hostname that carries a newline is exactly the shape that
should never reach URL construction.
"""
from __future__ import annotations

import json

from bridgebox.server import blobcast
from bridgebox.server.blobcast import BlobcastSessions, is_plain_hostname


def test_is_plain_hostname_no_longer_accepts_a_trailing_newline():
    # Regression: \Z only matches the true end of the string, unlike $ -
    # this "bare hostname safe to interpolate into a URL" check no longer
    # waves through a CRLF-shaped value.
    assert is_plain_hostname("evil.example.com\n") is False


def test_remember_no_longer_stores_the_newline_bearing_host():
    sessions = BlobcastSessions()
    sessions.remember("ecast-prod-use2.jackboxgames.com\n")
    # Regression: the guard now rejects it before it ever reaches the slot.
    assert sessions.upstream is None


def test_rewrite_room_response_hands_back_the_tainted_host_but_the_gate_now_stops_it():
    """rewrite_room_response itself still returns the raw field verbatim as
    "the real upstream host" (it has no reason to validate - it is not the
    trust boundary), but the one thing standing between that value and
    remember() storing it must now refuse it."""
    body = json.dumps({"server": "real-upstream.jackboxgames.com\n"}).encode("utf-8")
    _, real = blobcast.rewrite_room_response(body, local_host="127.0.0.1")
    assert real == "real-upstream.jackboxgames.com\n"
    assert is_plain_hostname(real) is False  # Regression: the gate now stops it


def test_a_clean_hostname_still_works_as_a_control():
    assert is_plain_hostname("ecast.jackboxgames.com") is True
    assert is_plain_hostname("evil.example.com\nHost: attacker") is False  # embedded, not trailing
