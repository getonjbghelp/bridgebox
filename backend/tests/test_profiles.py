"""Connection profiles: where each protocol is proxied to.

A profile is a destination preset - name, kind (ecast/blobcast) and upstream -
with one active per kind. Before this, both upstreams were constants, so
playing a Party Pack 1-6 game meant hand-editing rewrite.upstream_base to
blobcast.jackboxgames.com, which then broke every Party Pack 7+ game until it
was edited back.

The two kinds are never a choice between: their paths are disjoint (Ecast is
/api/v2/*, Blobcast is /room, /accessToken, /socket.io/*), so both are always
served. Built-in profiles cannot be deleted, which is what makes "no active
profile for this kind" an unreachable state rather than an error to handle.
"""
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from pydantic import ValidationError

from bridgebox import config, profiles_io
from bridgebox.config import Config, ProfilesConfig


# ---- schema ---------------------------------------------------------------


def test_the_official_servers_are_present_and_active_by_default():
    profiles = ProfilesConfig()

    assert profiles.active("ecast").upstream == "https://ecast.jackboxgames.com"
    assert profiles.active("blobcast").upstream == "https://blobcast.jackboxgames.com"


def test_builtins_cannot_be_deleted_so_a_kind_is_never_left_without_one():
    """The user asked for a warning when a protocol has no profile. Making
    that state unreachable is better than warning about it."""
    profiles = ProfilesConfig()

    assert [p.id for p in profiles.items if p.builtin] == ["official-ecast", "official-blobcast"]
    for kind in ("ecast", "blobcast"):
        assert profiles.active(kind) is not None


def test_an_active_id_that_no_longer_exists_falls_back_to_the_builtin():
    """A deleted profile must not take the bridge down with it - the game
    would fail with no visible cause."""
    profiles = ProfilesConfig(active_ecast="deleted-by-the-user")

    assert profiles.active("ecast").id == "official-ecast"


@pytest.mark.parametrize("bad", ["http://plain.example", "ftp://x", "not a url", ""])
def test_a_profile_upstream_must_be_https(bad):
    """Same reasoning as RewriteConfig's validator: this decides where the
    game's traffic, tokens included, is sent."""
    with pytest.raises(ValueError):
        ProfilesConfig(
            items=[{"id": "x", "name": "X", "kind": "ecast", "upstream": bad}]
        )


def test_a_custom_profile_can_be_selected():
    profiles = ProfilesConfig(
        items=list(ProfilesConfig().items)
        + [{"id": "mine", "name": "My server", "kind": "blobcast", "upstream": "https://my.example"}],
        active_blobcast="mine",
    )

    assert profiles.active("blobcast").upstream == "https://my.example"
    assert profiles.active("ecast").upstream == "https://ecast.jackboxgames.com"


# ---- migration ------------------------------------------------------------


def test_a_customised_legacy_upstream_becomes_the_active_ecast_profile():
    """rewrite.upstream_base was the only way to redirect the bridge before
    profiles existed. Silently ignoring a value the user had set would look
    exactly like the setting being reverted, which this repo has been bitten
    by before (Config.model_validate drops keys it does not know)."""
    config = Config.model_validate(
        {"rewrite": {"upstream_base": "https://my-mirror.example"}}
    )

    assert config.profiles.active("ecast").upstream == "https://my-mirror.example"


def test_an_explicit_profiles_section_wins_over_the_legacy_field():
    config = Config.model_validate(
        {
            "rewrite": {"upstream_base": "https://old.example"},
            "profiles": {
                "items": [
                    {"id": "new", "name": "New", "kind": "ecast", "upstream": "https://new.example"}
                ],
                "active_ecast": "new",
            },
        }
    )

    assert config.profiles.active("ecast").upstream == "https://new.example"


# ---- both protocols served at once ---------------------------------------


class _RecordingUpstream:
    def __init__(self):
        self.calls = []

    async def request(self, method, url, *, headers, data):
        from bridgebox.server.rooms import UpstreamResponse

        self.calls.append(url)
        # The two protocols spell "server" differently, and that difference is
        # load-bearing: Ecast returns a wss:// relay URL, Blobcast a bare
        # hostname. rooms.rewrite_server_field deliberately only touches the
        # former, which is what keeps the two rewriters off each other's field.
        server = (
            "wss://ecast-relay-prod-01.jackboxgames.com/ws"
            if "/api/" in url
            else "real.jackboxgames.com"
        )
        body = json.dumps({"create": True, "server": server}).encode()
        return UpstreamResponse(status=200, headers={"Content-Type": "application/json"}, body=body)


async def test_ecast_and_blobcast_are_served_at_once_from_different_upstreams():
    """The point of the whole feature, and previously only true by
    inspection: one running bridge, two protocols, two destinations, nothing
    to switch between them. A Party Pack 7 game and a Party Pack 3 game can
    be played back to back without touching a setting."""
    from bridgebox.server.factory import build_full_app

    upstream = _RecordingUpstream()
    profiles = ProfilesConfig(
        items=[
            {"id": "e", "name": "E", "kind": "ecast", "upstream": "https://ecast.example"},
            {"id": "b", "name": "B", "kind": "blobcast", "upstream": "https://blobcast.example"},
        ],
        active_ecast="e",
        active_blobcast="b",
    )
    app = build_full_app(
        host="127.0.0.1", port=8443, http_client=upstream, ws_connector=None, profiles=profiles
    )

    async with TestClient(TestServer(app)) as client:
        await client.get("/api/v2/rooms/ABCD")
        await client.get("/room")

    assert upstream.calls == [
        "https://ecast.example/api/v2/rooms/ABCD",
        "https://blobcast.example/room",
    ]


async def test_the_runtime_hands_the_profiles_to_the_bridge(tmp_path):
    """Without this the schema exists and the UI edits it, but the running
    bridge still uses the hardcoded upstreams - the setting would appear to
    save and change nothing, which is the failure mode this repo has hit
    before with silently-dropped config keys."""
    from bridgebox.runtime_core import RuntimeCore
    from tests.test_runtime_core import _make_deps

    calls, deps = _make_deps(tmp_path)
    config = Config()
    config.zapret.enabled = False
    config.profiles = ProfilesConfig(
        items=[
            {"id": "e", "name": "E", "kind": "ecast", "upstream": "https://picked-ecast.example"},
            {"id": "b", "name": "B", "kind": "blobcast", "upstream": "https://picked-blob.example"},
        ],
        active_ecast="e",
        active_blobcast="b",
    )
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)

    await core.start()
    try:
        passed = calls["build_full_app"][0]["profiles"]
        assert passed.active("ecast").upstream == "https://picked-ecast.example"
        assert passed.active("blobcast").upstream == "https://picked-blob.example"
    finally:
        await core.stop()


# ---- settings live inside the profile they belong to ----------------------


def test_ecast_settings_default_off_and_belong_to_the_profile():
    """Response rewriting only ever applied to Ecast, and was global while the
    bridge had a single destination. Two Ecast profiles - the official server
    and a mirror - are different situations that may need different rewriting,
    and one shared copy is how a setting appears not to work."""
    from bridgebox.config import ProfilesConfig

    profile = ProfilesConfig().active("ecast")

    assert profile.ecast.server_enabled is False
    assert profile.ecast.room_id_keys  # inherited from rooms.py, not re-typed
    assert not hasattr(profile.ecast, "upstream_base"), (
        "the address is Profile.upstream - a second field for it would drift"
    )


def test_blobcast_carries_its_own_socketio_port():
    from bridgebox.config import ProfilesConfig
    from bridgebox.server.blobcast import SOCKETIO_PORT

    assert ProfilesConfig().active("blobcast").blobcast.socketio_port == SOCKETIO_PORT


def test_the_whole_legacy_rewrite_section_migrates_not_just_the_address():
    """Somebody who had turned rewriting on would otherwise find it silently
    off - indistinguishable from the settings-reset failure this repo has
    been bitten by before."""
    config = Config.model_validate(
        {
            "rewrite": {
                "upstream_base": "https://mirror.example",
                "server_enabled": True,
                "user_agent_enabled": True,
                "upstream_origin": "https://origin.example",
            }
        }
    )

    ecast = config.profiles.active("ecast")
    assert ecast.upstream == "https://mirror.example"
    assert ecast.ecast.server_enabled is True
    assert ecast.ecast.user_agent_enabled is True
    assert ecast.ecast.upstream_origin == "https://origin.example"


async def test_the_bridge_rewrites_according_to_the_active_ecast_profile():
    """The setting has to reach the proxy, not merely be stored. Two Ecast
    profiles differing only in server_enabled must produce different
    behaviour, or the per-profile split is decorative."""
    from bridgebox.server.factory import build_full_app

    upstream = _RecordingUpstream()
    profiles = ProfilesConfig(
        items=[
            {
                "id": "e",
                "name": "E",
                "kind": "ecast",
                "upstream": "https://ecast.example",
                "ecast": {"server_enabled": True},
            },
            {"id": "b", "name": "B", "kind": "blobcast", "upstream": "https://blob.example"},
        ],
        active_ecast="e",
        active_blobcast="b",
    )
    app = build_full_app(
        host="127.0.0.1", port=8443, http_client=upstream, ws_connector=None, profiles=profiles
    )

    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/api/v2/rooms/ABCD")).json()

    # server_enabled on: the upstream's relay host is replaced with ours.
    assert body["server"].startswith("wss://127.0.0.1:8443")


async def test_a_custom_socketio_port_is_actually_listened_on(tmp_path):
    """Otherwise the field is decorative: stored, shown, and ignored - the
    failure mode this repo already knows from config keys that silently do
    nothing."""
    from bridgebox.runtime_core import RuntimeCore
    from bridgebox.server.factory import build_full_app
    from tests.test_runtime_core import _make_deps

    calls, deps = _make_deps(tmp_path)
    deps["build_full_app"] = lambda **kw: build_full_app(**kw)

    config = Config()
    config.zapret.enabled = False
    config.profiles = ProfilesConfig(
        items=[
            {"id": "e", "name": "E", "kind": "ecast", "upstream": "https://e.example"},
            {
                "id": "b",
                "name": "B",
                "kind": "blobcast",
                "upstream": "https://b.example",
                "blobcast": {"socketio_port": 45000},
            },
        ],
        active_ecast="e",
        active_blobcast="b",
    )
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)

    await core.start()
    try:
        assert [c["port"] for c in calls["run_server"]] == [config.server.port, 45000]
    finally:
        await core.stop()


# ---- the rest of what actually drives Blobcast ---------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "127.0.0.1:8443",        # the exact value that cost three game sessions
        "https://localhost",     # scheme
        "localhost/socket.io",   # path
        "",
        "has space",
    ],
)
def test_local_server_name_must_be_a_bare_hostname(bad):
    """The game appends :38203 to whatever this says, so anything carrying a
    port, a scheme or a path becomes an unresolvable name - measured, each
    stalling the game on its own repeatable delay with nothing reaching the
    bridge at all. Rejecting it here is cheaper than rediscovering it."""
    from bridgebox.config import BlobcastSettings

    with pytest.raises(ValueError):
        BlobcastSettings(local_server_name=bad)


@pytest.mark.parametrize("good", ["localhost", "bridge.local", "127.0.0.1"])
def test_a_bare_hostname_is_accepted(good):
    from bridgebox.config import BlobcastSettings

    assert BlobcastSettings(local_server_name=good).local_server_name == good


def test_blobcast_paths_follow_the_same_rule_as_the_proxy_paths():
    from bridgebox.config import BlobcastSettings

    assert BlobcastSettings(paths=["/room/", " /accessToken "]).paths == [
        "/room",
        "/accessToken",
    ]
    with pytest.raises(ValueError):
        BlobcastSettings(paths=["room"])       # no leading slash
    with pytest.raises(ValueError):
        BlobcastSettings(paths=[])             # nothing would route to Blobcast


def test_the_configured_paths_decide_what_counts_as_blobcast():
    """Otherwise the list is decorative and a non-official server with other
    routes cannot be reached."""
    from bridgebox.server.blobcast import is_blobcast_path

    assert is_blobcast_path("/room") is True
    assert is_blobcast_path("/custom", paths=("/custom",)) is True
    assert is_blobcast_path("/room", paths=("/custom",)) is False


async def test_interception_off_leaves_the_server_field_alone(tmp_path):
    """The escape hatch: rooms still get created, the session goes direct, and
    the second listener is not raised at all."""
    from bridgebox.runtime_core import RuntimeCore
    from bridgebox.server.factory import build_full_app
    from tests.test_runtime_core import _make_deps

    calls, deps = _make_deps(tmp_path)
    deps["build_full_app"] = lambda **kw: build_full_app(**kw)

    config = Config()
    config.zapret.enabled = False
    config.profiles = ProfilesConfig(
        items=[
            {"id": "e", "name": "E", "kind": "ecast", "upstream": "https://e.example"},
            {
                "id": "b",
                "name": "B",
                "kind": "blobcast",
                "upstream": "https://b.example",
                "blobcast": {"intercept_session": False},
            },
        ],
        active_ecast="e",
        active_blobcast="b",
    )
    core = RuntimeCore(config=config, project_root=tmp_path, **deps)

    await core.start()
    try:
        assert [c["port"] for c in calls["run_server"]] == [config.server.port], (
            "the socket.io listener must not be raised when interception is off"
        )
    finally:
        await core.stop()

    upstream = _RecordingUpstream()
    app = build_full_app(
        host="127.0.0.1", port=8443, http_client=upstream, ws_connector=None,
        profiles=config.profiles,
    )
    async with TestClient(TestServer(app)) as client:
        body = await (await client.get("/room")).json()

    assert body["server"] == "real.jackboxgames.com", "the field must pass through untouched"


# ---- changing an existing profile's type ---------------------------------


def test_changing_type_keeps_both_settings_blocks():
    """Switching there and back must not quietly wipe what was configured -
    both blocks exist on every profile precisely so this is lossless."""
    profiles = ProfilesConfig(
        items=[
            {
                "id": "mine",
                "name": "Mine",
                "kind": "ecast",
                "upstream": "https://mine.example",
                "ecast": {"server_enabled": True},
                "blobcast": {"socketio_port": 45000},
            }
        ],
        active_ecast="mine",
    )

    switched = profiles.model_copy(deep=True)
    switched.items[0].kind = "blobcast"

    assert switched.items[0].ecast.server_enabled is True
    assert switched.items[0].blobcast.socketio_port == 45000


def test_retyping_the_active_profile_leaves_its_old_kind_on_the_builtin():
    """The old kind would otherwise point at a profile that is no longer of
    that kind, and the bridge would have no destination for it."""
    profiles = ProfilesConfig(
        items=list(ProfilesConfig().items)
        + [{"id": "mine", "name": "Mine", "kind": "ecast", "upstream": "https://mine.example"}],
        active_ecast="mine",
    )

    profiles.items[-1].kind = "blobcast"

    assert profiles.active("ecast").id == "official-ecast"


def test_a_builtin_cannot_change_kind():
    """Built-ins are what make "this protocol has no destination"
    unreachable; letting one change kind would reintroduce it."""
    with pytest.raises(ValueError):
        ProfilesConfig(
            items=[
                {
                    "id": "official-ecast",
                    "name": "Официальный Ecast",
                    "kind": "blobcast",
                    "upstream": "https://ecast.jackboxgames.com",
                    "builtin": True,
                }
            ]
        )


# ---- forward_all/paths are the Ecast profile's own setting ---------------
#
# This used to be one global gate (ProxyConfig) checked BEFORE the Ecast/
# Blobcast split, so narrowing it could silently starve Blobcast too - even
# though Blobcast already had its own, separate paths list meant to be its
# whole scope. Moved into EcastSettings, the same shape Blobcast's paths
# already had, and the gate order in factory.py changes to match: Blobcast is
# classified and forwarded first, unconditionally, before Ecast's forward_all
# is ever consulted.


def test_ecast_forward_all_and_paths_default_like_the_old_global_setting():
    profile = ProfilesConfig().active("ecast")

    assert profile.ecast.forward_all is True
    assert profile.ecast.paths == ["/api", "/tts", "/media"]


def test_ecast_paths_follow_the_same_rule_as_blobcast_paths():
    from bridgebox.config import EcastSettings

    assert EcastSettings(paths=["/api/", " /tts "]).paths == ["/api", "/tts"]
    with pytest.raises(ValueError):
        EcastSettings(paths=["api"])  # no leading slash
    with pytest.raises(ValueError):
        EcastSettings(paths=[])  # forward_all off + empty paths = nothing reachable


async def test_narrowing_ecast_paths_does_not_starve_blobcast():
    """The bug this move fixes. Before it, this exact configuration - Ecast
    restricted to /api only - made every Blobcast request 404 at the shared
    gate before Blobcast's own path list was ever consulted."""
    from bridgebox.server.factory import build_full_app

    upstream = _RecordingUpstream()
    profiles = ProfilesConfig(
        items=[
            {
                "id": "e", "name": "E", "kind": "ecast", "upstream": "https://ecast.example",
                "ecast": {"forward_all": False, "paths": ["/api"]},
            },
            {"id": "b", "name": "B", "kind": "blobcast", "upstream": "https://blob.example"},
        ],
        active_ecast="e",
        active_blobcast="b",
    )
    app = build_full_app(
        host="127.0.0.1", port=8443, http_client=upstream, ws_connector=None, profiles=profiles
    )

    async with TestClient(TestServer(app)) as client:
        room = await client.get("/room")
        blocked = await client.get("/something/not/listed")

    assert room.status == 200
    assert upstream.calls == ["https://blob.example/room"]
    assert blocked.status == 404, "still refused - forward_all=False must still gate non-Blobcast paths"


# ---- carrying the legacy top-level proxy setting into the Ecast profile --


def test_a_customised_legacy_proxy_setting_migrates_into_every_ecast_profile():
    """The realistic case: profiles already exist (added in an earlier
    release) but predate these two fields, so the legacy top-level value has
    to reach the Ecast profile(s) rather than being silently dropped - the
    exact failure this repo has already been bitten by with config keys.

    The saved items are built by hand, WITHOUT forward_all/paths in their
    `ecast` dict, to simulate a real config.yaml written before those keys
    existed - going through a live ProfilesConfig().items dump would already
    carry the new schema's defaults and defeat the point of the test."""
    config = Config.model_validate(
        {
            "proxy": {"forward_all": False, "paths": ["/api"]},
            "profiles": {
                "items": [
                    {
                        "id": "official-ecast", "name": "Официальный Ecast", "kind": "ecast",
                        "upstream": "https://ecast.jackboxgames.com", "builtin": True,
                    },
                    {
                        "id": "official-blobcast", "name": "Официальный Blobcast", "kind": "blobcast",
                        "upstream": "https://blobcast.jackboxgames.com", "builtin": True,
                    },
                    {
                        "id": "mine", "name": "Mine", "kind": "ecast",
                        "upstream": "https://mine.example",
                    },
                ],
            },
        }
    )

    for profile in config.profiles.items:
        if profile.kind != "ecast":
            continue
        assert profile.ecast.forward_all is False
        assert profile.ecast.paths == ["/api"]


def test_migration_never_overwrites_an_already_customised_ecast_profile():
    config = Config.model_validate(
        {
            "proxy": {"forward_all": False, "paths": ["/api"]},
            "profiles": {
                "items": [
                    {
                        "id": "official-ecast", "name": "Официальный Ecast", "kind": "ecast",
                        "upstream": "https://ecast.jackboxgames.com", "builtin": True,
                        "ecast": {"forward_all": True, "paths": ["/api", "/tts", "/media"]},
                    },
                ],
            },
        }
    )

    ecast = config.profiles.active("ecast")
    assert ecast.ecast.forward_all is True
    assert ecast.ecast.paths == ["/api", "/tts", "/media"]


def test_default_legacy_proxy_settings_do_not_trigger_a_migration():
    """proxy.forward_all/paths at their own defaults is not "customised" -
    nothing should be written that a fresh profile would not already have."""
    config = Config.model_validate({"profiles": {"items": [p.model_dump() for p in ProfilesConfig().items]}})

    assert config.profiles.active("ecast").ecast.forward_all is True
    assert config.profiles.active("ecast").ecast.paths == ["/api", "/tts", "/media"]


# ---- upstream addresses are parsed, not prefix-matched ----


@pytest.mark.parametrize(
    "hostile",
    [
        # Everything before "@" is userinfo: this resolves to evil.example.com
        # while reading, in Settings, as the official Jackbox address. It is the
        # one shape that defeats profiles_io's whole reason for existing.
        "https://ecast.jackboxgames.com@evil.example.com",
        "https://user:pass@evil.example.com",
        # Callers append their own path to this value, so a path here lands in
        # the middle of the URL they build.
        "https://evil.example.com/redirect?to=",
        "https://evil.example.com/#",
        "http://ecast.jackboxgames.com",
        "https://",
    ],
)
def test_a_profile_upstream_that_hides_its_real_host_is_refused(hostile):
    with pytest.raises(ValidationError):
        config.Profile(id="p", name="p", kind="ecast", upstream=hostile)


def test_an_imported_profile_cannot_smuggle_a_userinfo_upstream():
    """The import rules promise a shared file can add destinations without
    redirecting traffic. A userinfo host defeats that by looking official."""
    payload = {
        "format": "bridgebox-profiles",
        "version": 1,
        "profiles": [
            {
                "id": "looks-official",
                "name": "Официальный Ecast",
                "kind": "ecast",
                "upstream": "https://ecast.jackboxgames.com@evil.example.com",
            }
        ],
    }
    result, report = profiles_io.import_payload(payload, into=config.ProfilesConfig())

    assert report["added"] == 0
    assert len(report["skipped"]) == 1
    assert all(p.builtin for p in result.items)


def test_a_normal_upstream_still_loads():
    profile = config.Profile(
        id="p", name="p", kind="ecast", upstream="https://ecast.jackboxgames.com/"
    )
    assert profile.upstream == "https://ecast.jackboxgames.com"


# ---- the bridge only ever listens on loopback ----


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_are_accepted(host):
    assert config.ServerConfig(host=host).host == host


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "::", "example.com"])
def test_a_non_loopback_bind_is_refused(host):
    # config.yaml is user-writable and sits next to the binary. This value binds
    # both the bridge and Blobcast's socket.io listener, and neither
    # authenticates anything - relay.resolve_room hands any WS connection to the
    # only known room.
    with pytest.raises(ValidationError):
        config.ServerConfig(host=host)
