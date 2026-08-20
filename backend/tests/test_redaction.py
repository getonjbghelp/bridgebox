"""Credentials must not reach the log.

The leak this pins (H3): the room token is not carried in a JSON body when it
matters - it is a QUERY PARAMETER, because that is the authorisation scheme
Jackbox actually accepts. `redact` only ever matched "key": "value", so every
URL carrying ?token=... went to the log verbatim, at INFO, on the request line
of every proxied call and every WS upgrade.

That log is written to disk, shown behind a "Копировать" button, and exported
as .log/.json/.html specifically so it can be pasted into a bug report - so the
leak ends with the credential in a stranger's hands.
"""
import ast
import re
from pathlib import Path

import pytest

from bridgebox.server.rooms import SENSITIVE_BODY_KEYS, redact

SERVER_DIR = Path(__file__).resolve().parents[1] / "bridgebox"


# ---- what redact() must cover -------------------------------------------


def test_a_token_in_a_query_string_is_hidden():
    """The exact shape desktop._close_test_room sends, which is the shape the
    live API was measured to accept."""
    line = "/api/v2/rooms/ABCD?token=super-secret-room-token"

    assert "super-secret-room-token" not in redact(line)
    assert "token=" in redact(line), "the parameter name must survive"


def test_every_sensitive_key_is_covered_in_both_transports():
    """One list, two shapes. A key protected in a body but not in a URL is the
    bug this whole module exists for."""
    for key in SENSITIVE_BODY_KEYS:
        body = f'{{"{key}": "SECRETVALUE"}}'
        query = f"/api/v2/rooms/X?{key}=SECRETVALUE"

        assert "SECRETVALUE" not in redact(body), f"{key} leaks in a JSON body"
        assert "SECRETVALUE" not in redact(query), f"{key} leaks in a query string"


def test_redaction_survives_a_full_url_and_several_parameters():
    url = "https://ecast.jackboxgames.com/api/v2/rooms/ABCD?userId=U-1&token=T-2&role=host"
    out = redact(url)

    assert "U-1" not in out
    assert "T-2" not in out
    # Non-secret parameters are left readable - the log is still meant to be
    # useful, and over-redacting teaches people to ignore it.
    assert "role=host" in out


def test_the_match_is_anchored_to_a_parameter_boundary():
    """Without the ?/& anchor this would also eat the middle of a word, and a
    log line that mangles unrelated text is one nobody trusts."""
    assert redact("/api/v2/mytokenstore?x=1") == "/api/v2/mytokenstore?x=1"


def test_a_value_stops_at_the_parameter_it_belongs_to():
    out = redact("/x?token=abc&next=keepme")

    assert "abc" not in out
    assert "keepme" in out


def test_case_does_not_matter():
    assert "S3CRET" not in redact("/x?Token=S3CRET")
    assert "S3CRET" not in redact("/x?AUTHTOKEN=S3CRET")


def test_redact_never_raises_on_junk():
    """It runs on previews truncated mid-document, so malformed input is the
    normal case, not the exception."""
    for junk in ("", "?", "&&&", '{"token": ', "?token=", "%%%", "?token=�"):
        redact(junk)  # must not raise


# ---- truncate-then-redact: the ordering used to leak a partial secret ---


def test_a_token_straddling_the_preview_truncation_boundary_is_still_hidden():
    """SECURITY FIX. _preview used to truncate the raw body to a fixed
    character limit BEFORE calling redact() - see rooms._preview's old
    `redact(body[:limit].decode(...))`. The JSON-shaped half of redact()'s
    regex, _SECRET_RE, requires a CLOSING quote to match:
    `"token"(\\s*:\\s*)"[^"]*"`. When the truncation cut a sensitive value in
    half, the closing quote fell outside the slice redact() ever saw, the
    regex silently failed to match, and everything up to the cut point - part
    of a real room token - was written to the log verbatim.

    Not a contrived value: BODY_PREVIEW_CHARS is 800 in production, upstream
    field order is Jackbox's choice not ours, and a "token" key landing near
    that boundary in a real Ecast/Blobcast payload is exactly the kind of
    thing this module's own docstring says must never reach the log ("That
    log is written to disk, shown behind a 'Копировать' button, and exported
    ... so the leak ends with the credential in a stranger's hands.").

    Fixed by redacting the whole body before truncating, so the token is
    long gone before any cut point can land inside it - the same shape of
    fix already applied once for the query-string half of this exact leak
    (H3)."""
    from bridgebox.server.rooms import _preview

    secret = "SUPERSECRETROOMTOKEN1234567890ABCDEF"
    pad = "x" * 50
    body = ('{"ok": true, "body": {"pad": "%s", "token": "%s"}}' % (pad, secret)).encode()

    # Cut the preview right in the middle of the secret's value, before its
    # closing quote - not a crafted edge case, just where an 800-char limit
    # would land on a slightly longer real response.
    cut = body.index(secret.encode()) + 10
    rendered = _preview(body, limit=cut, content_type="application/json")

    assert secret[:10] not in rendered, "a real token must never appear in a log preview, even partially"
    assert secret not in rendered


def test_a_frame_preview_has_the_same_truncate_after_redact_ordering():
    """Same fix, second call site - relay.py's WS frame logging reuses
    redact() the same way rooms.py's HTTP body preview does. Ecast and
    Blobcast frames carry the same "token" field, at log level INFO when
    log_frames is on."""
    from bridgebox.server.relay import _frame_preview

    secret = "SUPERSECRETROOMTOKEN1234567890ABCDEF"
    pad = "x" * 50
    frame = '{"ok": true, "body": {"pad": "%s", "token": "%s"}}' % (pad, secret)

    cut = frame.index(secret) + 10
    rendered = _frame_preview(frame, limit=cut)

    assert secret[:10] not in rendered, "a real token must never appear in a frame preview, even partially"
    assert secret not in rendered


# ---- and that no log line bypasses it -----------------------------------


# The two request attributes that carry a query string. request.path does not,
# which is why it is absent here.
TAINTED_ATTRS = {"path_qs", "query_string"}

# Files whose logging is audited. The whole server package plus desktop.py,
# which is the other place that formats upstream URLs.
_AUDITED = ["server/rooms.py", "server/relay.py", "server/blobcast.py", "server/app.py",
            "server/factory.py", "desktop.py"]


def _log_calls(tree: ast.AST):
    """Every logger.<level>(...) call in a module."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        target = node.func.value
        if isinstance(target, ast.Name) and target.id in ("logger", "console_logger"):
            yield node


def _mentions_tainted(node: ast.AST) -> bool:
    """Whether an expression reads request.path_qs / request.query_string."""
    return any(
        isinstance(child, ast.Attribute) and child.attr in TAINTED_ATTRS
        for child in ast.walk(node)
    )


def _is_redacted(node: ast.AST) -> bool:
    """Whether the expression is wrapped in redact(...)."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "redact"
    )


@pytest.mark.parametrize("relative", _AUDITED)
def test_no_log_line_passes_a_raw_query_string(relative: str):
    """A source scan, not a behaviour test, and that is the point: the leak was
    never a broken function - it was six call sites that reached for the raw
    attribute. Only a rule that reads the source can stop the seventh."""
    path = SERVER_DIR / relative
    tree = ast.parse(path.read_text(encoding="utf-8"))

    offenders = []
    for call in _log_calls(tree):
        for argument in call.args:
            if _mentions_tainted(argument) and not _is_redacted(argument):
                offenders.append(f"{relative}:{argument.lineno}")

    assert not offenders, (
        "these log arguments carry a query string without redact(): "
        + ", ".join(offenders)
    )


def test_the_scanner_itself_catches_a_raw_query_string(tmp_path: Path):
    """A guard that cannot fail is not a guard. This is the mutation the test
    above is supposed to catch, run against the scanner directly."""
    source = "logger.info('ws %s', request.query_string)\n"
    tree = ast.parse(source)

    found = [
        call
        for call in _log_calls(tree)
        for argument in call.args
        if _mentions_tainted(argument) and not _is_redacted(argument)
    ]

    assert found, "the scanner would not have noticed the original leak"


def test_the_scanner_accepts_a_redacted_one():
    tree = ast.parse("logger.info('ws %s', redact(request.query_string))\n")

    found = [
        call
        for call in _log_calls(tree)
        for argument in call.args
        if _mentions_tainted(argument) and not _is_redacted(argument)
    ]

    assert not found


def test_the_token_query_scheme_is_still_what_the_code_sends():
    """If _close_test_room ever stops using a query parameter, the regex above
    is guarding a shape nothing produces - and the real one goes unguarded.
    Pinned by reading the source rather than by running a live request."""
    source = (SERVER_DIR / "desktop.py").read_text(encoding="utf-8")

    assert re.search(r'params\s*=\s*\{"token":', source), (
        "the room token is no longer sent as a query parameter - check that "
        "redact() still covers whatever replaced it"
    )
