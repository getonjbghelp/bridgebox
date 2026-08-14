"""RewriteConfig: the knobs that used to be literals in rooms.py.

The drift guards at the bottom are the load-bearing part. Every other rewrite
test in this repo (test_rooms.py, test_rooms_robust.py - 500 lines between
them) calls rewrite_server_field without a `rewrite` argument, so they only
keep testing production behaviour for as long as the no-config path and the
default-config path stay identical.
"""

import json

import pytest
from bridgebox.config import Config, RewriteConfig
from bridgebox.server import rooms
from bridgebox.server.rooms import RoomsProxy, UpstreamResponse, rewrite_server_field

LOCAL_WS = "wss://127.0.0.1:8443/ws"

# The shape a real room creation returns (apptag "fourbage", captured live):
# envelope-wrapped, bare relay hostname under "host", code under "code".
HOST_SHAPE = json.dumps(
    {
        "ok": True,
        "body": {
            "host": "ecast-prod-use2.jackboxgames.com",
            "code": "MNAK",
            "token": "670f3779de7658e56fb5306e",
        },
    }
).encode("utf-8")

SERVER_SHAPE = json.dumps(
    {"roomid": "ABCD", "server": "wss://ecast-relay-prod-01.jackboxgames.com/ws"}
).encode("utf-8")


# ---- server rewriting module ----------------------------------------------


def test_disabled_passes_the_body_through_but_still_finds_the_room_code():
    """The room code has to survive server_enabled=False: test_connection
    reads it out of a real creation response and has no interest in
    rewriting anything. If disabling rewriting short-circuited the whole
    walk, diagnostics would break the moment a user turned it off."""
    body, server, room_id = rewrite_server_field(
        SERVER_SHAPE, local_ws_base=LOCAL_WS, rewrite=RewriteConfig(server_enabled=False)
    )

    assert body == SERVER_SHAPE  # byte-identical, not a re-serialized equivalent
    assert server is None
    assert room_id == "ABCD"


def test_server_rewriting_is_off_by_default():
    """A direct connection already works through the DPI bypass (zapret's own
    hostlist), so rewriting the response is opt-in, not the default path."""
    body, server, room_id = rewrite_server_field(
        SERVER_SHAPE, local_ws_base=LOCAL_WS, rewrite=RewriteConfig()
    )

    assert body == SERVER_SHAPE
    assert server is None
    assert room_id == "ABCD"


def test_server_rewriting_when_explicitly_enabled():
    body, server, room_id = rewrite_server_field(
        SERVER_SHAPE, local_ws_base=LOCAL_WS, rewrite=RewriteConfig(server_enabled=True)
    )

    assert json.loads(body)["server"] == LOCAL_WS
    assert server == "wss://ecast-relay-prod-01.jackboxgames.com/ws"
    assert room_id == "ABCD"


def test_a_bare_host_is_never_rewritten_even_with_rewriting_enabled():
    """host_keys / "server+host" mode used to make this configurable; removed
    after confirming (twice) that rewriting "host" only breaks a working
    direct connection - see the comment on rooms._walk_and_rewrite."""
    body, server, _ = rewrite_server_field(
        HOST_SHAPE, local_ws_base=LOCAL_WS, rewrite=RewriteConfig(server_enabled=True)
    )

    assert body == HOST_SHAPE
    assert server is None


# ---- configurable keys --------------------------------------------------


def test_custom_server_key_is_rewritten_and_the_default_one_is_not():
    body = json.dumps(
        {"wsUrl": "wss://relay.example.com/ws", "server": "wss://other.example.com/ws"}
    ).encode("utf-8")

    new_body, server, _ = rewrite_server_field(
        body,
        local_ws_base=LOCAL_WS,
        rewrite=RewriteConfig(server_enabled=True, server_keys=["wsUrl"]),
    )

    parsed = json.loads(new_body)
    assert parsed["wsUrl"] == LOCAL_WS
    assert parsed["server"] == "wss://other.example.com/ws"  # no longer in server_keys
    assert server == "wss://relay.example.com/ws"


def test_custom_room_id_key_is_found_and_the_default_one_is_not():
    body = json.dumps({"gameCode": "QRST", "roomid": "IGNORED"}).encode("utf-8")

    _, _, room_id = rewrite_server_field(
        body, local_ws_base=LOCAL_WS, rewrite=RewriteConfig(room_id_keys=["gameCode"])
    )

    assert room_id == "QRST"


# ---- proxy headers ------------------------------------------------------


class RecordingClient:
    def __init__(self):
        self.calls = []

    async def request(self, method, url, *, headers, data):
        self.calls.append({"url": url, "headers": dict(headers)})
        return UpstreamResponse(status=200, headers={}, body=b"{}")


async def test_proxy_sends_the_configured_origin_and_user_agent():
    client = RecordingClient()
    rewrite = RewriteConfig(
        origin_enabled=True,
        upstream_origin="https://staging.example.com",
        user_agent_enabled=True,
        fallback_user_agent="BridgeBox-Test/1.0",
    )
    proxy = RoomsProxy(
        upstream_base="https://ecast.jackboxgames.com",
        local_ws_base=LOCAL_WS,
        http_client=client,
        room_relays={},
        rewrite=rewrite,
    )

    await proxy.forward("POST", "/api/v2/rooms", headers={}, data=b"{}")

    headers = client.calls[0]["headers"]
    assert headers["Origin"] == "https://staging.example.com"
    assert headers["User-Agent"] == "BridgeBox-Test/1.0"


async def test_proxy_still_prefers_the_games_own_user_agent():
    """fallback_user_agent is always just a fallback - the game's own UA is
    the one the load balancer is happiest with."""
    client = RecordingClient()
    proxy = RoomsProxy(
        upstream_base="https://ecast.jackboxgames.com",
        local_ws_base=LOCAL_WS,
        http_client=client,
        room_relays={},
        rewrite=RewriteConfig(user_agent_enabled=True, fallback_user_agent="BridgeBox-Test/1.0"),
    )

    await proxy.forward(
        "POST", "/api/v2/rooms", headers={"User-Agent": "JackboxGame/1.2"}, data=b"{}"
    )

    assert client.calls[0]["headers"]["User-Agent"] == "JackboxGame/1.2"


def _proxy_with(rewrite: RewriteConfig) -> tuple[RoomsProxy, RecordingClient]:
    client = RecordingClient()
    return (
        RoomsProxy(
            upstream_base="https://ecast.jackboxgames.com",
            local_ws_base=LOCAL_WS,
            http_client=client,
            room_relays={},
            rewrite=rewrite,
        ),
        client,
    )


async def test_origin_module_off_forwards_the_games_own_origin_untouched():
    """Off has to mean "don't touch it", not "drop it": the request is still
    stripped of the *incoming* Origin (which names this bridge) only when
    there is a replacement to put back."""
    proxy, client = _proxy_with(RewriteConfig(origin_enabled=False))

    await proxy.forward(
        "POST", "/api/v2/rooms", headers={"Origin": "https://jackbox.tv"}, data=b"{}"
    )

    assert client.calls[0]["headers"]["Origin"] == "https://jackbox.tv"


async def test_origin_module_off_sends_no_origin_when_the_game_sent_none():
    proxy, client = _proxy_with(RewriteConfig(origin_enabled=False))

    await proxy.forward("POST", "/api/v2/rooms", headers={}, data=b"{}")

    assert not any(k.lower() == "origin" for k in client.calls[0]["headers"])


async def test_user_agent_module_off_injects_nothing():
    proxy, client = _proxy_with(RewriteConfig(user_agent_enabled=False))

    await proxy.forward("POST", "/api/v2/rooms", headers={}, data=b"{}")

    assert not any(k.lower() == "user-agent" for k in client.calls[0]["headers"])


async def test_user_agent_module_off_still_forwards_the_games_own():
    proxy, client = _proxy_with(RewriteConfig(user_agent_enabled=False))

    await proxy.forward(
        "POST", "/api/v2/rooms", headers={"User-Agent": "JackboxGame/1.2"}, data=b"{}"
    )

    assert client.calls[0]["headers"]["User-Agent"] == "JackboxGame/1.2"


async def test_modules_are_independent():
    """The point of separate switches: turning one off must not disturb the
    others. Host is never optional either way."""
    proxy, client = _proxy_with(
        RewriteConfig(origin_enabled=False, user_agent_enabled=True, fallback_user_agent="UA/1.0")
    )

    await proxy.forward("POST", "/api/v2/rooms", headers={}, data=b"{}")

    headers = client.calls[0]["headers"]
    assert headers["User-Agent"] == "UA/1.0"  # still on
    assert not any(k.lower() == "origin" for k in headers)  # off
    assert headers["Host"] == "ecast.jackboxgames.com"  # never gated


# ---- validation ---------------------------------------------------------


@pytest.mark.parametrize("value", ["http://ecast.jackboxgames.com", "", "ftp://x.example", "https://"])
def test_upstream_base_rejects_anything_that_is_not_an_https_url(value):
    with pytest.raises(ValueError):
        RewriteConfig(upstream_base=value)


def test_upstream_base_is_normalised():
    assert RewriteConfig(upstream_base="  https://x.example/  ").upstream_base == "https://x.example"


def test_blank_and_whitespace_keys_are_rejected_or_dropped():
    assert RewriteConfig(server_keys=["server", "", "  "]).server_keys == ["server"]

    with pytest.raises(ValueError):
        RewriteConfig(server_keys=["two words"])


# ---- drift guards -------------------------------------------------------


def test_defaults_match_the_constants_they_came_from():
    """RewriteConfig imports these rather than re-typing them; this fails if
    someone inlines a copy and the two drift."""
    defaults = RewriteConfig()

    assert defaults.room_id_keys == list(rooms.ROOM_ID_KEYS)
    assert defaults.upstream_base == rooms.UPSTREAM_BASE
    assert defaults.upstream_origin == rooms.UPSTREAM_ORIGIN
    assert defaults.fallback_user_agent == rooms.FALLBACK_USER_AGENT


@pytest.mark.parametrize("body", [SERVER_SHAPE, HOST_SHAPE, b"not json at all"])
def test_no_config_is_identical_to_a_fully_enabled_config(body):
    """The contract that keeps test_rooms.py and test_rooms_robust.py
    meaningful: they all call without `rewrite`, so that path has to agree
    with *something* nameable, or it silently drifts into untested behaviour.

    Not RewriteConfig() any more: the schema default flipped to off (a direct
    connection already works through the DPI bypass, so rewriting is opt-in),
    but `rewrite=None` is a separate, frozen "predates RewriteConfig" default
    for legacy callers - production never actually passes None (RuntimeCore
    always hands RoomsProxy a real Config().rewrite). Pinning it to the
    fully-on shape is what it always meant in practice, now made explicit."""
    assert rewrite_server_field(body, local_ws_base=LOCAL_WS) == rewrite_server_field(
        body, local_ws_base=LOCAL_WS, rewrite=RewriteConfig(server_enabled=True)
    )


def test_rewrite_section_round_trips_through_the_top_level_config():
    dumped = Config().model_dump()

    assert dumped["rewrite"]["server_enabled"] is False
    assert Config.model_validate(dumped).rewrite == RewriteConfig()


def test_a_null_patch_resets_a_section_to_its_defaults():
    """What the Settings "Сбросить" button sends. A null has to *remove* the
    key so pydantic refills the default - setting it to None would fail
    validation, since no field here is nullable."""
    from bridgebox.desktop import _deep_merge

    merged = Config(rewrite=RewriteConfig(server_enabled=False, server_keys=["wsUrl"])).model_dump()
    _deep_merge(merged, {"rewrite": None})

    assert Config.model_validate(merged).rewrite == RewriteConfig()


def test_a_null_leaf_resets_only_that_field():
    from bridgebox.desktop import _deep_merge

    merged = Config(
        rewrite=RewriteConfig(server_enabled=False, upstream_origin="https://x.example")
    ).model_dump()
    _deep_merge(merged, {"rewrite": {"upstream_origin": None}})

    result = Config.model_validate(merged).rewrite
    assert result.upstream_origin == rooms.UPSTREAM_ORIGIN  # back to the default
    assert result.server_enabled is False  # untouched
