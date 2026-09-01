from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .paths import PROJECT_ROOT
from .server.blobcast import (
    BLOBCAST_PREFIXES,
    BLOBCAST_UPSTREAM,
    LOCAL_SERVER_NAME,
    SOCKETIO_PORT,
)
from .server.rooms import (
    FALLBACK_USER_AGENT,
    ROOM_ID_KEYS,
    UPSTREAM_BASE,
    UPSTREAM_ORIGIN,
)

logger = logging.getLogger(__name__)


class TlsConfig(BaseModel):
    cert_dir: str = "certs/"


# The bridge authenticates nothing. It assumes a connection reaching it came
# from the game on this machine, and relay.resolve_room will hand ANY WS
# connection to the single known room when the query params don't name one.
# Both of those are safe on loopback and only on loopback.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=8443, ge=1, le=65535)
    tls: TlsConfig = Field(default_factory=TlsConfig)

    @field_validator("host")
    @classmethod
    def _must_stay_on_loopback(cls, value: str) -> str:
        # config.yaml ships next to the binary in a user-writable folder, which
        # is the same reasoning zapret.dir already carries a validator for. This
        # value binds TWO listeners (the bridge and Blobcast's socket.io site on
        # 38203, see runtime_core.start), and one edit to "0.0.0.0" turns both
        # into an unauthenticated relay into Jackbox for anyone on the network.
        # Nothing needs a non-loopback bridge: players reach the game through
        # jackbox.tv, never through this address.
        host = value.strip()
        if host not in LOOPBACK_HOSTS:
            raise ValueError(
                "мост слушает только на локальном адресе (127.0.0.1, ::1 или localhost)"
            )
        return host


class ZapretConfig(BaseModel):
    enabled: bool = True
    dir: str = "zapret"
    strategy: str = "general"
    # On by default: winws's console is a second window the user never asked
    # for and cannot usefully read, and BridgeBox is meant to be one window.
    # Its output is not lost - the bridge log carries everything that matters.
    hide_console: bool = True

    @field_validator("dir")
    @classmethod
    def _must_stay_inside_the_install(cls, value: str) -> str:
        # This directory is where a .bat gets *executed* from, by a process
        # that main() requires to be running as Administrator. That makes it a
        # strictly higher-value trust boundary than upstream_base, which is
        # already validated below.
        #
        # BridgeBox ships portable, so config.yaml sits next to the binary in
        # a user-writable folder. Without this, any local user could point
        # this at a directory they control, drop a strategies\*.bat there, and
        # wait for an administrator to launch the app - textbook local
        # privilege escalation from one edited line of YAML.
        candidate = (PROJECT_ROOT / value).resolve()
        if not candidate.is_relative_to(PROJECT_ROOT.resolve()):
            raise ValueError("zapret dir must stay inside the BridgeBox folder")
        return value


class PathsConfig(BaseModel):
    """Where BridgeBox puts working files it does not keep.

    Deliberately NOT constrained to the install tree the way zapret.dir is:
    nothing here is ever executed, every path written under it is constructed
    by us rather than taken from an archive, and the point of exposing it is
    letting the user park a download on a drive that has room. An empty value
    means the system temp directory."""

    temp_dir: str = "temp"

    @field_validator("temp_dir")
    @classmethod
    def _must_be_usable(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return value  # system temp
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        if candidate.exists() and not candidate.is_dir():
            raise ValueError("путь для временных файлов занят файлом, а не папкой")
        return value


class UpdateConfig(BaseModel):
    """Checking Flowseal for a newer zapret payload.

    Off by default: GitHub is frequently unreachable from the networks this
    app exists for, and a blocked check on every launch would hang startup and
    fill the log for a feature most users will run once. The Settings toggle
    is there for anyone who wants it."""

    check_on_startup: bool = False


class AppUpdateConfig(BaseModel):
    """Checking GitHub for a newer BridgeBox release - see app_update.py.

    Off by default, per product decision: even though this is also how a
    security-critical fix (see app_update.CRITICAL_MARKER) would reach
    somebody who never opens Settings, a fresh install must not phone GitHub
    before the user has chosen to. It is also never blocking (see desktop.Api.
    start_app_update_check's startup delay), so turning it on later costs an
    unreachable GitHub nothing but a silently failed background request."""

    check_on_startup: bool = False
    # The last version whose update modal the user has already seen and
    # closed - suppresses the MODAL for that version on future launches so
    # it does not nag every time. Deliberately does NOT gate the critical
    # banner/reminder (see HomeScreen's UpdateBanner): a critical release
    # must not become permanently silenceable by a single click, which is
    # the whole point of calling it critical.
    dismissed_version: str = ""


class HealthCheckConfig(BaseModel):
    """The background reachability re-check that runs while the bridge is up
    - see runtime_core.RuntimeCore._health_check_loop. Separate from a
    user-triggered "Проверить соединение": this is the same targets_for()
    ping repeated silently every couple of minutes, so a provider's DPI
    change surfaces as a banner instead of the user only finding out mid-game.

    On by default: it is a read-only probe of the game's own servers, and its
    first round also does the job a separate one-shot upstream pre-warm used
    to (see _health_check_loop's own docstring for why that was folded in
    here rather than kept apart). Read only at bridge start, same as
    strategy/port/profile - see RESTART_SCOPE in SettingsScreen.tsx."""

    enabled: bool = True


class LoggingConfig(BaseModel):
    level: Literal["debug", "info", "warning", "error"] = "info"
    dir: str = "logs/"
    rotate_mb: int = Field(default=20, gt=0)


class UiConfig(BaseModel):
    # SetupWizard.tsx forces 'dark' on every first run regardless of this
    # value ("a first run is dark, per the wizard's own default") - matching
    # that here means the native title bar's very first paint (window.shown,
    # driven straight off this config, no round trip) already agrees with it
    # instead of racing the async update_config() call that used to correct
    # it a beat later, which is what produced a white title bar over dark
    # content for that first second.
    theme: Literal["light", "dark"] = "dark"
    animations_enabled: bool = True
    # The one dial every hook in frontend/src/lib/motion.ts scales its own
    # (otherwise-hardcoded) duration against, so turning it moves every
    # animation together instead of retuning each transition by hand. Bounds
    # keep it from going either imperceptible or sluggish: below ~50ms reads
    # as a glitch rather than motion, above 1s reads as the UI hanging.
    animation_duration_ms: int = Field(default=220, ge=50, le=1000)
    # "system" resolves on the frontend from navigator.language, once, and the
    # result is what's actually shown - this field only remembers the user's
    # PREFERENCE (follow the OS, or a deliberate override), never a resolved
    # locale, so a machine that changes its Windows language does not silently
    # drag an explicit "ru" choice back to English underneath the user.
    language: Literal["system", "ru", "en"] = "system"
    # Lives here rather than in localStorage: theme and animations are already
    # config.yaml settings, and a second persistence mechanism for the third
    # preference in the same panel is how the two get out of step.
    # Collapsed by default: the rail's three destinations are one glyph each,
    # and the screen this app is actually about is the toggle on Запуск. The
    # expanded rail spends 232px of a 960px window on labels for a menu the
    # user reads once.
    sidebar_collapsed: bool = True
    # Whether the first-run wizard has been through to its last screen. False
    # by default, so a machine with no config.yaml (the shipped state - see
    # save_config's "lazy save on first change") gets the wizard, and so does
    # a factory reset: that sends `ui: null`, which drops the key and lets
    # this default refill it. Deliberately not its own top-level section -
    # the frontend already reads the whole `ui` block on mount for the theme,
    # so the wizard's gate costs no extra bridge call.
    setup_complete: bool = False
    # Start with Windows, via a Task Scheduler task rather than a Run key.
    # main() exits without Administrator and zapret/WinDivert cannot work
    # without it, so a Run key would either die at every boot or raise a UAC
    # prompt every single time - see bridgebox.autostart.
    autostart: bool = False
    # Whether that autostart lands in the tray instead of showing the window.
    autostart_minimized: bool = False
    # Turn the bridge on as soon as the app opens, so a player who leaves
    # autostart on never has to press anything.
    start_bridge_on_launch: bool = False
    # Closing the window hides to the tray instead of quitting. On by default:
    # the bridge is only useful while it runs, and a window closed by reflex
    # used to take the whole bypass down mid-game.
    # OFF by default. Closing a window is the one gesture every user already
    # knows the meaning of, and quietly redefining it to "hide" is how an app
    # ends up still running when somebody believed they had quit it. Anyone
    # who wants the bypass to survive a stray click can turn it on.
    minimize_to_tray: bool = False
    # Dismissing the "files were modified" banner for good. Its own flag rather
    # than a generic "hide warnings": this one is meant to be dismissable by
    # somebody who edits their own strategies on purpose and knows exactly why
    # the banner is there, and nothing else should ride along with that choice.
    integrity_warning_dismissed: bool = False


class ProxyConfig(BaseModel):
    """Which request paths the bridge forwards upstream.

    Legacy: superseded by EcastSettings.forward_all/paths on the active Ecast
    profile (see _carry_over_the_legacy_proxy_paths), kept in the schema only
    so a config.yaml written before profiles existed migrates instead of
    silently losing a customised value - the same reasoning RewriteConfig is
    still here for. Nothing user-facing reads this section any more.

    forward_all ships **on**, which is what the bridge already did before this
    section existed. The game treats this bridge as its entire server, so a
    path outside any list we thought to write down is not an error - it is an
    endpoint nobody had seen yet. FixyText's POST /tts/generate is the worked
    example: an /api-only allowlist answered it with the browser warning page,
    and the game read an HTML document where it expected generated speech.

    Turning it off narrows the bridge to `paths`. Anything else is refused
    with a JSON 404 and a WARNING naming the path, so the next endpoint this
    breaks says so out loud instead of failing the way /tts/generate did.
    """

    forward_all: bool = True
    # /api and /tts are both confirmed from real traffic. /media is not - it is
    # seeded here because the PRD's media proxy (Hear Say voice clips, avatars)
    # would land somewhere like it, and an unused prefix in this list costs
    # nothing while a missing one costs a broken game.
    paths: list[str] = Field(default_factory=lambda: ["/api", "/tts", "/media"])

    @field_validator("paths")
    @classmethod
    def _must_be_absolute_path_prefixes(cls, value: list[str]) -> list[str]:
        return _clean_path_prefixes(
            value, empty_error="список путей пуст — включите «весь трафик» или добавьте путь"
        )


class RewriteConfig(BaseModel):
    """How Ecast API responses get rewritten on the way back to the game.

    Every default is imported from server/rooms.py rather than re-typed here.
    Each of those constants carries a paragraph of empirically-earned comment
    (the 403-without-User-Agent finding, the unverified room-code spelling),
    and a second copy of the literal is how the two drift apart.

    Three independent switches, each next to the value it gates, so one can
    be turned off without disturbing the others. All three ship off: a
    direct connection already works through the DPI bypass (see
    rooms._walk_and_rewrite's comment on why "host" is never rewritten), so
    rewriting the response is an opt-in tool for re-testing that, not the
    default path.

    server_enabled      rewrite a "server" key holding a ws(s):// URL. Off
                        passes the body through byte-identical.
    origin_enabled      replace the game's Origin header with our own. Off
                        forwards whatever Origin the game sent, untouched.
    user_agent_enabled  supply a browser-like User-Agent when the game sent
                        none. Off sends the request without one - expect a
                        403 HTML page from the load balancer (see
                        rooms.FALLBACK_USER_AGENT).

    Two values deliberately have no switch:

    upstream_base   where /api/** is forwarded. The bridge has to forward
                    somewhere; "off" would either be a no-op or break it.
    room_id_keys    finding the room code never alters the response - it is
                    a read, feeding diagnostics and WS relay routing. Off
                    would change nothing for the game while breaking
                    test_connection, which fails outright without a code.

    A bare relay hostname under "host" is never rewritten - see the comment
    on rooms._walk_and_rewrite for why. This used to be a third mode
    ("server+host"); removed after confirming it only breaks a working
    direct connection.
    """

    server_enabled: bool = False
    server_keys: list[str] = Field(default_factory=lambda: ["server"])
    room_id_keys: list[str] = Field(default_factory=lambda: list(ROOM_ID_KEYS))
    upstream_base: str = UPSTREAM_BASE
    origin_enabled: bool = False
    upstream_origin: str = UPSTREAM_ORIGIN
    user_agent_enabled: bool = False
    fallback_user_agent: str = FALLBACK_USER_AGENT

    @field_validator("upstream_base", "upstream_origin")
    @classmethod
    def _must_be_https_url(cls, value: str) -> str:
        # This decides where the game's traffic - including whatever auth
        # headers it carries - actually goes, and it is editable from a UI in
        # a process running as Administrator. That makes it a trust boundary,
        # not a preference: no http://, no free-form scheme, and no userinfo
        # hiding the real host behind a plausible-looking one.
        return _clean_https_origin(value, error="must be an https:// URL")

    @field_validator("server_keys", "room_id_keys")
    @classmethod
    def _no_blank_keys(cls, value: list[str]) -> list[str]:
        # A stray "" would match no JSON key but silently widen the walk; a
        # key with whitespace is always a typo from the comma-separated UI.
        cleaned = [key.strip() for key in value if key.strip()]
        if any(" " in key for key in cleaned):
            raise ValueError("JSON keys cannot contain spaces")
        return cleaned


ProfileKind = Literal["ecast", "blobcast"]

BLOBCAST_UPSTREAM_DEFAULT = BLOBCAST_UPSTREAM
BUILTIN_ECAST_ID = "official-ecast"
BUILTIN_BLOBCAST_ID = "official-blobcast"


class EcastSettings(BaseModel):
    """Response rewriting, plus which paths this profile actually forwards -
    both only ever applying to Ecast.

    These were global (RewriteConfig, ProxyConfig) while the bridge had one
    destination. They belong to a profile: pointing Ecast at a mirror and at
    the official server are different situations that may need different
    rewriting, and a global copy silently applied to both is how a setting
    appears not to work.

    forward_all/paths used to be checked BEFORE the Ecast/Blobcast split, so
    narrowing them could silently starve Blobcast too, even though Blobcast
    already had its own, separate paths list meant to be its whole scope.
    factory.py now classifies and forwards Blobcast first, unconditionally;
    these two only ever gate what is left.

    upstream_base is deliberately absent - Profile.upstream is the address,
    and two fields for one thing is how they drift apart."""

    server_enabled: bool = False
    server_keys: list[str] = Field(default_factory=lambda: ["server"])
    room_id_keys: list[str] = Field(default_factory=lambda: list(ROOM_ID_KEYS))
    origin_enabled: bool = False
    upstream_origin: str = UPSTREAM_ORIGIN
    user_agent_enabled: bool = False
    fallback_user_agent: str = FALLBACK_USER_AGENT

    forward_all: bool = True
    # /api and /tts are both confirmed from real traffic. /media is not - it is
    # seeded here because the PRD's media proxy (Hear Say voice clips, avatars)
    # would land somewhere like it, and an unused prefix in this list costs
    # nothing while a missing one costs a broken game.
    paths: list[str] = Field(default_factory=lambda: ["/api", "/tts", "/media"])

    @field_validator("paths")
    @classmethod
    def _must_be_absolute_path_prefixes(cls, value: list[str]) -> list[str]:
        return _clean_path_prefixes(
            value, empty_error="список путей пуст — включите «весь трафик» или добавьте путь"
        )


def _clean_path_prefixes(value: list[str], *, empty_error: str) -> list[str]:
    """Shared by ProxyConfig.paths and BlobcastSettings.paths - two copies of
    this rule would be two chances for them to disagree about what a path is."""
    cleaned = []
    for raw in value:
        path = raw.strip()
        if not path:
            continue
        if not path.startswith("/"):
            raise ValueError(f"путь должен начинаться с «/»: {path!r}")
        if any(char.isspace() for char in path):
            raise ValueError(f"путь не может содержать пробелы: {path!r}")
        # Stored without a trailing slash so matching is one rule: exact, or
        # followed by "/" (see rooms.path_is_forwarded).
        cleaned.append(path.rstrip("/") or "/")
    if not cleaned:
        raise ValueError(empty_error)
    return cleaned


def _clean_https_origin(value: str, *, error: str) -> str:
    """Validate a bare https:// server address, the way a URL parser reads it.

    A startswith("https://") check is not enough, and the gap is not
    theoretical: "https://ecast.jackboxgames.com@evil.example.com" passes it,
    displays in Settings as the official address, and resolves to
    evil.example.com - everything before the "@" is userinfo, not the host.
    That single trick defeats the entire point of profiles_io's import rules,
    which exist so a shared file can offer destinations without being able to
    silently redirect the traffic (room tokens included).

    A path/query/fragment is refused too: callers append their own path to
    this value ("{upstream_base}{path}"), so anything here would land in the
    middle of the resulting URL rather than where the author expected."""
    cleaned = value.strip().rstrip("/")
    parsed = urlsplit(cleaned)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(error)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(
            "адрес не может содержать «@» — настоящий сервер в таком адресе стоит после неё"
        )
    if parsed.path or parsed.query or parsed.fragment:
        raise ValueError("нужен только адрес сервера, без пути и параметров")
    return cleaned


class BlobcastSettings(BaseModel):
    """What is Blobcast's alone. Every one of these was a constant in
    server/blobcast.py, and every one of them has already proved it matters.

    socketio_port is the port the GAME opens its session on, found by packet
    capture. Changing it has a consequence outside this file: the zapret
    strategies carry the port in their --wf-tcp/--filter-tcp filters, and a
    session on a port missing from those gets no DPI bypass at all - measured,
    the first connection succeeds and every repeat times out.

    intercept_session off passes the "server" field through untouched and
    raises no second listener, so the session goes straight to Jackbox. The
    room still gets created; we simply cannot see the session. An escape
    hatch, not a feature.

    log_frames puts the relayed socket.io frames in the log at info. They are
    logged at debug otherwise, which means reading the protocol required
    raising the GLOBAL level and drowning every other line - a cost already
    paid once."""

    # Every default is imported from server/blobcast.py rather than re-typed,
    # for the same reason RewriteConfig imports its own from server/rooms.py:
    # each of those constants carries a paragraph of empirically-earned comment
    # (the packet capture that found the port, the three sessions burned on a
    # non-bare hostname), and a second copy of the literal is how the two
    # silently drift apart.
    socketio_port: int = Field(default=SOCKETIO_PORT, ge=1, le=65535)
    intercept_session: bool = True
    local_server_name: str = LOCAL_SERVER_NAME
    log_frames: bool = False
    paths: list[str] = Field(default_factory=lambda: list(BLOBCAST_PREFIXES))

    @field_validator("local_server_name")
    @classmethod
    def _must_be_a_bare_hostname(cls, value: str) -> str:
        # The game appends ":<socketio_port>" to this itself, so a port, a
        # scheme or a path here produces a name that resolves to nothing. That
        # is not a hypothetical: "127.0.0.1:8443" and "https://127.0.0.1:8443"
        # were both tried against the real game and each stalled it on its own
        # repeatable delay with nothing ever reaching the bridge.
        name = value.strip()
        if not name:
            raise ValueError("имя хоста не может быть пустым")
        if "://" in name or "/" in name:
            raise ValueError("нужно голое имя хоста, без схемы и пути")
        if ":" in name:
            raise ValueError("нужно голое имя хоста, без порта — игра добавит порт сама")
        if any(char.isspace() for char in name):
            raise ValueError("имя хоста не может содержать пробелы")
        return name

    @field_validator("paths")
    @classmethod
    def _must_be_absolute_path_prefixes(cls, value: list[str]) -> list[str]:
        return _clean_path_prefixes(
            value, empty_error="список путей Blobcast пуст — этому профилю ничего не достанется"
        )


class Profile(BaseModel):
    """One destination the bridge can proxy a protocol to, with the settings
    that only make sense for that protocol.

    `kind` is not a mode the user switches between - both protocols are always
    served, because their paths never collide. It says which half of the
    traffic this destination receives, and therefore which settings block
    below applies.

    Both blocks are always present rather than modelled as a discriminated
    union on `kind`. The unused one costs a few inert lines of YAML; a union
    would cost every reader, the null-unset reset patch and pydantic's error
    messages a special case, for no behaviour gained."""

    id: str
    name: str
    kind: ProfileKind
    upstream: str
    ecast: EcastSettings = Field(default_factory=EcastSettings)
    blobcast: BlobcastSettings = Field(default_factory=BlobcastSettings)
    # Built-ins cannot be deleted. That is what makes "this protocol has no
    # profile" unreachable rather than a state to warn about.
    builtin: bool = False

    @field_validator("upstream")
    @classmethod
    def _must_be_https_url(cls, value: str) -> str:
        # Same trust boundary as RewriteConfig.upstream_base: this decides
        # where the game's traffic - room tokens included - is sent. An
        # imported profile is the hostile case this has to survive, so the
        # value is parsed as a URL rather than prefix-matched.
        return _clean_https_origin(
            value, error="адрес сервера должен начинаться с https://"
        )

    @model_validator(mode="after")
    def _builtins_keep_their_kind(self) -> "Profile":
        """A built-in is the guarantee that its protocol always has somewhere
        to go - that is the whole reason they cannot be deleted. Letting one
        change kind would bring back the state the guarantee exists to
        prevent, just by a different route."""
        expected = {BUILTIN_ECAST_ID: "ecast", BUILTIN_BLOBCAST_ID: "blobcast"}.get(self.id)
        if self.builtin and expected and self.kind != expected:
            raise ValueError(f"встроенный профиль {self.id} не может сменить тип")
        return self


def _builtin_profiles() -> list[Profile]:
    return [
        Profile(
            id=BUILTIN_ECAST_ID,
            name="Официальный Ecast",
            kind="ecast",
            upstream=UPSTREAM_BASE,
            builtin=True,
        ),
        Profile(
            id=BUILTIN_BLOBCAST_ID,
            name="Официальный Blobcast",
            kind="blobcast",
            upstream=BLOBCAST_UPSTREAM_DEFAULT,
            builtin=True,
        ),
    ]


class ProfilesConfig(BaseModel):
    """Where each protocol is proxied to.

    Before this, both upstreams were constants and the only way to reach a
    Party Pack 1-6 game was hand-editing rewrite.upstream_base - which then
    broke every Party Pack 7+ game until it was edited back. Now each kind
    has its own active destination and the two coexist permanently."""

    items: list[Profile] = Field(default_factory=_builtin_profiles)
    active_ecast: str = BUILTIN_ECAST_ID
    active_blobcast: str = BUILTIN_BLOBCAST_ID

    def active(self, kind: ProfileKind) -> Profile:
        """The destination for `kind`, never None.

        Falls back to the built-in if the active id names something that was
        deleted: a dangling reference must not take the bridge down, because
        the game would then fail with nothing on screen to explain it."""
        wanted = self.active_ecast if kind == "ecast" else self.active_blobcast
        by_id = {profile.id: profile for profile in self.items}
        chosen = by_id.get(wanted)
        if chosen is not None and chosen.kind == kind:
            return chosen

        for profile in self.items:
            if profile.kind == kind:
                return profile
        # items was emptied entirely - rebuild rather than raise, for the
        # same reason as above.
        return next(p for p in _builtin_profiles() if p.kind == kind)


def rewrite_for(profile: Profile) -> RewriteConfig:
    """The RewriteConfig an Ecast profile's settings describe.

    One place, because there were about to be two: factory.py builds this to
    hand RoomsProxy the shape it already takes, and diagnostics needs the same
    values to test the configuration the bridge is actually running. Reading
    the legacy top-level `rewrite` section there instead - which is what
    test_connection did - meant the check silently exercised schema defaults
    the moment anyone customised their profile.

    EcastSettings carries forward_all/paths, which RewriteConfig has no
    business knowing about, so fields are selected rather than splatted."""
    fields = {
        key: value
        for key, value in profile.ecast.model_dump().items()
        if key in RewriteConfig.model_fields
    }
    return RewriteConfig(**fields, upstream_base=profile.upstream)


class Config(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    zapret: ZapretConfig = Field(default_factory=ZapretConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    ui: UiConfig = Field(default_factory=UiConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    update: UpdateConfig = Field(default_factory=UpdateConfig)
    app_update: AppUpdateConfig = Field(default_factory=AppUpdateConfig)
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    profiles: ProfilesConfig = Field(default_factory=ProfilesConfig)
    rewrite: RewriteConfig = Field(default_factory=RewriteConfig)

    @model_validator(mode="before")
    @classmethod
    def _carry_over_the_legacy_upstream(cls, data):
        """Adopt a customised rewrite.upstream_base as the active Ecast
        profile, for configs written before profiles existed.

        That field was the only way to redirect the bridge, so somebody who
        had set it would otherwise see it silently stop applying - which in
        this repo looks exactly like the settings-reset failure that
        Config.model_validate's habit of dropping unknown keys has caused
        before. An explicit profiles section always wins; this only fills a
        gap."""
        if not isinstance(data, dict) or data.get("profiles"):
            return data

        # `rewrite` arrives as a dict when loading YAML, but as a built
        # RewriteConfig when a settings patch is re-validated from a dump -
        # both paths reach this validator, so neither may be assumed.
        rewrite = data.get("rewrite")
        if rewrite is None:
            return data
        legacy = rewrite if isinstance(rewrite, dict) else rewrite.model_dump()
        if not legacy:
            return data

        data = dict(data)
        items = [p.model_dump() for p in _builtin_profiles()]
        for item in items:
            if item["kind"] != "ecast":
                continue
            if isinstance(legacy.get("upstream_base"), str):
                item["upstream"] = legacy["upstream_base"]
            # Every rewrite setting travels, not just the address: somebody who
            # had turned response rewriting on would otherwise find it silently
            # off, which looks exactly like the settings-reset failure this
            # repo already knows Config.model_validate causes by dropping keys
            # it does not recognise.
            item["ecast"] = {
                key: value
                for key, value in legacy.items()
                if key in EcastSettings.model_fields
            }
        data["profiles"] = {"items": items}
        logger.info("migrated the legacy rewrite section into the built-in Ecast profile")
        return data

    @model_validator(mode="before")
    @classmethod
    def _carry_over_the_legacy_proxy_paths(cls, data):
        """Adopt a customised top-level proxy.forward_all/paths into every
        Ecast-kind profile that does not already have its own.

        Unlike _carry_over_the_legacy_upstream, this does NOT skip when
        `profiles` is already present: the profiles section was added in an
        earlier release, so a real config.yaml upgrading straight to this one
        already has a `profiles:` block - it just predates these two fields on
        `ecast`. Detected by their absence there, not by whether profiles
        exists at all. Runs after _carry_over_the_legacy_upstream, so
        `data["profiles"]["items"]` already reflects that migration if it
        applied, or an already-existing profiles section either way.

        Never overwrites a profile that already sets forward_all/paths of its
        own - that is what makes this naturally one-shot: the first
        Config.model_validate that runs after upgrade writes both keys into
        every ecast profile via model_dump, so every later load or patch
        merge already has them and this is a no-op."""
        if not isinstance(data, dict):
            return data

        # `proxy` arrives as a dict when loading YAML, but as a built
        # ProxyConfig when a settings patch is re-validated from a dump -
        # same reasoning as `rewrite` above.
        proxy = data.get("proxy")
        if proxy is None:
            return data
        legacy = proxy if isinstance(proxy, dict) else proxy.model_dump()
        if not legacy:
            return data

        profiles = data.get("profiles")
        if not isinstance(profiles, dict) or not isinstance(profiles.get("items"), list):
            return data  # nothing to attach the legacy value to yet

        changed = False
        items = []
        for item in profiles["items"]:
            if not isinstance(item, dict) or item.get("kind") != "ecast":
                items.append(item)
                continue
            item = dict(item)
            existing_ecast = item.get("ecast")
            existing_ecast = dict(existing_ecast) if isinstance(existing_ecast, dict) else {}
            if "forward_all" not in existing_ecast and "forward_all" in legacy:
                existing_ecast["forward_all"] = legacy["forward_all"]
                changed = True
            if "paths" not in existing_ecast and "paths" in legacy:
                existing_ecast["paths"] = legacy["paths"]
                changed = True
            if existing_ecast:
                item["ecast"] = existing_ecast
            items.append(item)

        if changed:
            data = dict(data)
            data["profiles"] = {**profiles, "items": items}
            logger.info("migrated the legacy proxy forward_all/paths into the Ecast profile(s)")
        return data


def load_config(path: str | Path) -> Config:
    """Load config from a YAML file, falling back to defaults for any
    field the file doesn't set. A missing file yields all-defaults."""
    path = Path(path)
    if not path.exists():
        logger.info("no config at %s - using defaults", path)
        return Config()

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    config = Config.model_validate(raw)
    # Which keys the file actually set vs. which came from defaults is the
    # first thing worth knowing when a setting appears not to apply.
    logger.info("loaded config from %s (file keys: %s)", path, ", ".join(sorted(raw)) or "<empty>")
    logger.debug("effective config: %s", config.model_dump())
    return config


def save_config(config: Config, path: str | Path) -> None:
    """Write config to a human-editable YAML file (used by Settings' lazy
    "save on first change" flow - see PRD "config.yaml не поставляется
    стартовым файлом")."""
    path = Path(path)
    # Write a sibling, then rename: os.replace is atomic within a volume, so a
    # crash mid-write leaves the previous config intact instead of a truncated
    # one. Same pattern as strategies.save_hostlist and update.apply_update -
    # this file holds every setting the user has ever changed, and it was the
    # one of the three written non-atomically.
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(yaml.safe_dump(config.model_dump(), sort_keys=False), encoding="utf-8")
    tmp.replace(path)
    logger.info("saved config to %s", path)
    logger.debug("saved config contents: %s", config.model_dump())


def _fill_missing_defaults(raw: dict, defaults: dict) -> tuple[dict, bool]:
    """Adds any key `defaults` has that `raw` is missing, recursively through
    nested sections, and never touches a key `raw` already has - even one
    set to None, [], or {}, since those are a deliberate choice (None in
    particular is this app's own "reset this section" convention - see
    update_config's _deep_merge) and not the same thing as "missing".

    Returns the merged dict and whether anything was actually added, so a
    caller writing the result back to disk can skip that write when a
    version bump added no new config fields."""
    changed = False
    merged = dict(raw)
    for key, default_value in defaults.items():
        if key not in merged:
            merged[key] = default_value
            changed = True
            continue
        if isinstance(default_value, dict) and isinstance(merged[key], dict):
            merged[key], nested_changed = _fill_missing_defaults(merged[key], default_value)
            changed = changed or nested_changed
    return merged, changed


def migrate_config_file(path: str | Path) -> bool:
    """Add any config field a newer BridgeBox version introduced to an
    EXISTING config.yaml, without touching a single value the user already
    set. Called once at startup, after load_config.

    This is the other half of "installing a new version must not reset the
    user's settings": load_config already defaults a field missing from the
    file in MEMORY (pydantic does that for free), so nothing was ever at
    risk of silently reverting - what was missing, until this, was the new
    field showing up in the FILE ITSELF for a user who edits it by hand or
    diffs it against a backup.

    A config.yaml that does not exist yet is left alone and NOT created here
    - this app ships with none (see save_config's docstring), and creating
    one on a machine that has never changed a setting would be new,
    unrequested behaviour. Returns whether anything was written, purely so a
    caller can log it; nothing depends on the return value."""
    path = Path(path)
    if not path.exists():
        return False

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    merged, changed = _fill_missing_defaults(raw, Config().model_dump())
    if not changed:
        return False

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")
    tmp.replace(path)
    logger.info("added new default config fields to %s", path)
    return True
