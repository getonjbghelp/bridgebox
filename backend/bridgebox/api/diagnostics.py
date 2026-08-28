""""Проверить соединение" - the connection-test diagnostic (reachability
ping + real room create/lookup/close round trip). See desktop.py's own
docstring on the mixin split. Not to be confused with bridgebox/diagnostics.py,
the module this borrows probe_targets/describe_exception/... from - that one
holds the zapret strategy-suite engine, this one is just its Api wrapper for
a single ad-hoc connection check."""
from __future__ import annotations

import json
import logging
import uuid

import aiohttp

from .. import i18n
from ..config import rewrite_for
from ..diagnostics import BLOBCAST_TARGETS, ECAST_TARGETS, describe_exception, probe_targets
from ..paths import resolve_project_path
from ..server.rooms import redact, rewrite_server_field
from ..tls.ca import CA_CERT_FILENAME

logger = logging.getLogger(__name__)

# A real, currently-active apptag confirmed against live traffic (see the
# room-creation shape rewrite_server_field/rooms.py handle) - used purely to
# exercise the room-create + room-lookup round trip; test_connection stops
# there and does not attempt a WS relay connect (see _test_connection_coro).
TEST_APPTAG = "fourbage"


def _redacted_json(value) -> str:
    """Render a decoded API payload for a UI step, credentials blanked.

    The room-creation response carries "token" - the credential that controls
    the room - and these strings are shown in the диагностика popup and
    routinely pasted into bug reports. Interpolating the parsed body raw put
    that token on screen, bypassing the SENSITIVE_BODY_KEYS discipline that
    already covers the log for exactly this payload."""
    return redact(json.dumps(value, ensure_ascii=False, default=str))


def _find_key(node, key: str) -> str | None:
    """First string stored under `key` at any depth. The creation response
    wraps its payload ({"ok":true,"body":{...}}), so a flat top-level lookup
    misses - the same reason rewrite_server_field walks the whole document
    instead of reading fixed keys."""
    if isinstance(node, dict):
        for name, value in node.items():
            if name == key and isinstance(value, str):
                return value
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None


class DiagnosticsMixin:
    def test_connection(self) -> dict:
        try:
            return self._runtime.run(self._test_connection_coro, timeout=20)
        except Exception as exc:
            return {"ok": False, "error": str(exc), "steps": []}

    async def _test_connection_coro(self) -> dict:
        """Two checks in one pass:

        1. A plain reachability ping of the real Ecast AND Blobcast hosts
           (ECAST_TARGETS + BLOBCAST_TARGETS - Ecast's API entry point plus a
           relay shard confirmed from live traffic; Blobcast's own API entry
           point alone), so a DPI block shows up immediately as its own step
           instead of being indistinguishable from a room-creation failure
           below. Blobcast is pinged here but not exercised beyond that - the
           room-creation round trip after it is Ecast-only, a much larger
           existing feature this ping addition isn't trying to duplicate.
        2. The room-creation round trip through the bridge itself: create a
           real room via our own /api/v2/rooms (exercises the outbound
           proxy + rewrite), then confirm it registered via
           GET /api/v2/rooms/<code> (both confirmed against the live API).

        Deliberately stops there - no WS relay connect. Confirmed against
        the live API that the actual relay upgrade gets rejected (403) no
        matter what query params/headers accompany it, for reasons still
        unknown; a check that reliably fails for an unrelated, unsolved
        reason is worse than no check, since it reads as "the bridge is
        broken" regardless of whether anything here actually is.

        Uses TEST_APPTAG ("fourbage", confirmed active against live traffic)
        plus a fresh userId (the create call is rejected outright without
        one - confirmed against the live API, previously silently missing
        here since every earlier check of this path used a fake upstream
        that never enforced it)."""
        import ssl

        lang = self.current_language()
        steps: list[str] = []
        status = self._runtime.get_status()
        if not status.get("running"):
            return {"ok": False, "error": i18n.t("diag.bridge_not_running", lang), "steps": steps}

        port = status["port"]
        # Verified against our own CA rather than CERT_NONE. The bridge's leaf
        # carries 127.0.0.1 as a SAN and the CA is right there on disk, so
        # turning verification off bought nothing and quietly meant "проверить
        # соединение" could not have noticed a broken certificate - which is
        # one of the things it exists to check.
        cert_dir = resolve_project_path(
            self._project_root, self._config.server.tls.cert_dir
        )
        ca_file = cert_dir / CA_CERT_FILENAME
        if ca_file.exists():
            ssl_context = ssl.create_default_context(cafile=str(ca_file))
        else:
            # The bridge is running, so the CA should exist; if it somehow does
            # not, say so instead of silently downgrading to no verification.
            steps.append(i18n.t("diag.no_ca_file", lang, name=ca_file.name))
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        # The profile the bridge is actually serving, not the legacy top-level
        # `rewrite` section - see config.rewrite_for.
        rewrite = rewrite_for(self._config.profiles.active("ecast"))

        async with aiohttp.ClientSession() as session:
            # Purely informational - a failed ping doesn't stop the test, so
            # the room-creation step below still runs and can show whether
            # the API host specifically is reachable even if the relay shard
            # isn't (or vice versa).
            ping_results = await probe_targets(session, ECAST_TARGETS + BLOBCAST_TARGETS)
            for name, result in ping_results.items():
                if result["ok"]:
                    steps.append(
                        i18n.t(
                            "diag.ping_ok",
                            lang,
                            name=name,
                            status=result["status"],
                            ms=f"{result['elapsedMs']:.0f}",
                        )
                    )
                else:
                    steps.append(i18n.t("diag.ping_error", lang, name=name, error=result["error"]))

            # A browser-like User-Agent is mandatory, not cosmetic: Jackbox's
            # AWS load balancer answers anything else with a 403 HTML page
            # before the request reaches Ecast (see rooms.py). aiohttp sends
            # its own "Python/3.x aiohttp/3.x" by default, and the proxy
            # forwards a UA that is already present rather than substituting
            # the fallback - so this test was reliably getting HTML back and
            # reporting the resulting JSONDecodeError as "ошибка сети".
            # Confirmed against the live API just now: a create-room request
            # with no userId is rejected outright - {"ok": false, "error":
            # "invalid parameters: missing required field userId"} - which
            # this test was silently sending until now (every earlier
            # verification of this code path used a fake upstream that never
            # enforced the real server's required fields). A fresh id per
            # run, same shape Jackbox's own client generates.
            user_id = str(uuid.uuid4()).upper()
            try:
                async with session.post(
                    f"https://127.0.0.1:{port}/api/v2/rooms",
                    json={"apptag": TEST_APPTAG, "userId": user_id},
                    headers={"User-Agent": rewrite.fallback_user_agent},
                    ssl=ssl_context,
                ) as response:
                    status = response.status
                    raw = await response.read()
            except Exception as exc:
                steps.append(
                    i18n.t("diag.create_room_network_error", lang, detail=describe_exception(exc))
                )
                return {"ok": False, "error": steps[-1], "steps": steps}

            try:
                body = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Report what actually came back instead of a bare parse
                # error - HTTP status plus a body snippet is the difference
                # between "DPI is blocking us" and "the API changed shape".
                snippet = redact(raw[:200].decode("utf-8", errors="replace").strip())
                steps.append(
                    i18n.t(
                        "diag.create_room_not_json",
                        lang,
                        status=status,
                        n=len(raw),
                        snippet=repr(snippet),
                    )
                )
                return {"ok": False, "error": steps[-1], "steps": steps}

            if not isinstance(body, dict):
                steps.append(
                    i18n.t(
                        "diag.create_room_unexpected_json",
                        lang,
                        status=status,
                        json=_redacted_json(body)[:200],
                    )
                )
                return {"ok": False, "error": steps[-1], "steps": steps}

            # Reuses the exact function RoomsProxy uses in production, rather
            # than re-checking body.get("roomid") here: the real API wraps
            # the payload in {"ok":..., "body": {...}} and names the room
            # code "code", not "roomid" - a flat top-level lookup for those
            # two keys reported "no room" on every real room creation, which
            # is what "ответ без комнаты/relay" actually was.
            local_ws_base = f"wss://127.0.0.1:{port}/ws"
            _, server, room_id = rewrite_server_field(
                raw, local_ws_base=local_ws_base, rewrite=rewrite
            )

            if not room_id:
                steps.append(
                    i18n.t(
                        "diag.create_room_no_code",
                        lang,
                        status=status,
                        keys=tuple(rewrite.room_id_keys),
                        json=_redacted_json(body)[:400],
                    )
                )
                return {"ok": False, "error": steps[-1], "steps": steps}

            # Whether a "server"/"host" relay field was found and rewritten
            # is purely informational here - this test no longer opens a WS
            # relay connection (see the coro's docstring), so a room lacking
            # one is not a failure, just a note about this particular app.
            relay_note = f", relay -> {server}" if server else ""
            steps.append(
                i18n.t("diag.room_created", lang, room_id=room_id, status=status, relay_note=relay_note)
            )

            # Confirmed against the live API: GET /api/v2/rooms/<code> right
            # after creation returns 200 with the room's full state (host,
            # audienceHost, locked, full, maxPlayers, ...) - a real
            # second-step lookup a game client can rely on, not just a POST
            # response the room could theoretically forget. A failure here
            # means the room didn't actually register server-side, which the
            # POST response alone can't tell you.
            try:
                async with session.get(
                    f"https://127.0.0.1:{port}/api/v2/rooms/{room_id}",
                    headers={"User-Agent": rewrite.fallback_user_agent},
                    ssl=ssl_context,
                ) as lookup_response:
                    lookup_status = lookup_response.status
                    await lookup_response.read()
            except Exception as exc:
                steps.append(
                    i18n.t(
                        "diag.room_check_network_error",
                        lang,
                        room_id=room_id,
                        detail=describe_exception(exc),
                    )
                )
                return {"ok": False, "error": steps[-1], "steps": steps}

            if lookup_status != 200:
                steps.append(
                    i18n.t("diag.room_check_bad_status", lang, room_id=room_id, status=lookup_status)
                )
                return {"ok": False, "error": steps[-1], "steps": steps}
            steps.append(i18n.t("diag.room_confirmed", lang, room_id=room_id))

            await self._close_test_room(
                session,
                port,
                room_id,
                ssl_context,
                steps,
                token=_find_key(body, "token"),
                user_agent=rewrite.fallback_user_agent,
            )

        return {"ok": True, "error": None, "steps": steps}

    async def _close_test_room(
        self,
        session,
        port,
        room_id,
        ssl_context,
        steps: list,
        *,
        token: str | None = None,
        user_agent: str,
    ) -> None:
        """Tear the test room back down. The PRD asked for "создание комнаты/
        разрушение комнаты"; only the create half was ever implemented, so
        every click of "Проверить соединение" left a real room behind on
        Jackbox's own servers. They very likely expire on their own (nothing
        ever opens a host WS to them), but creating them by the dozen and
        never cleaning up is load on someone else's infrastructure that this
        diagnostic has no business generating.

        Strictly best-effort, and deliberately never fails the test: by the
        time this runs the check has already proved everything it set out to.

        The token goes in the QUERY STRING, not a header. That is measured,
        not assumed: probed against the real API, `Authorization: Bearer
        <token>` and a bare `Authorization: <token>` both answer
        403 {"ok":false,"error":"bad token"}, and `?token=<token>` answers
        200 "ok". Every room this diagnostic created before that was found
        stayed open on Jackbox's servers - 12 real attempts in the log, zero
        successes."""
        lang = self.current_language()
        headers = {"User-Agent": user_agent}
        params = {"token": token} if token else {}

        try:
            async with session.delete(
                f"https://127.0.0.1:{port}/api/v2/rooms/{room_id}",
                headers=headers,
                params=params,
                ssl=ssl_context,
            ) as response:
                status = response.status
                raw = await response.read()
        except Exception as exc:
            steps.append(
                i18n.t(
                    "diag.room_close_failed_network",
                    lang,
                    room_id=room_id,
                    detail=describe_exception(exc),
                )
            )
            logger.warning("could not close test room %s: %s", room_id, exc)
            return

        if 200 <= status < 300:
            steps.append(i18n.t("diag.room_closed", lang, room_id=room_id, status=status))
            return

        # Report what the server actually said. Saying "not supported" here
        # was wrong: the server's own answer was "bad token", which is a
        # different problem with a different fix.
        detail = redact(raw[:120].decode("utf-8", errors="replace").strip())
        steps.append(
            i18n.t("diag.room_close_failed_status", lang, room_id=room_id, status=status, detail=detail)
        )
        logger.info("DELETE of test room %s answered HTTP %s: %s", room_id, status, detail)
