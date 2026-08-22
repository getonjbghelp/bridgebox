"""Find installed Jackbox Steam titles and write/restore the
`-jbg.config serverUrl=...` launch option BridgeBox's README otherwise asks
the user to paste in by hand.

Steam-only - see docs/superpowers/specs/2026-08-21-steam-launch-options-design.md
for the "Прочие копии" scope decision. Detection is local/offline: matches
an installed title's Steam manifest name against "jackbox" case-insensitively,
plus a small bundled list of singles whose Steam title doesn't say Jackbox at
all - keeping this current as new Jackbox singles ship is a known, accepted
maintenance cost of not calling out to a live list.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import winreg as _real_winreg
from dataclasses import dataclass
from pathlib import Path

from . import steam_vdf

logger = logging.getLogger(__name__)


def _read_vdf_text(path: Path) -> str:
    """Bytes decoded with surrogateescape, never errors="replace" - Steam's
    localconfig.vdf can hold a stray non-UTF-8 byte anywhere (a legacy
    Latin-1 persona name, say) that has nothing to do with this feature.
    surrogateescape round-trips it losslessly; "replace" would permanently
    stamp it out the moment this module writes the file back."""
    return path.read_bytes().decode("utf-8", errors="surrogateescape")


def _write_vdf_text(path: Path, text: str) -> None:
    """Bytes out (not write_text) so LF line endings survive untouched on
    Windows instead of being translated to CRLF file-wide, plus the same
    sibling-temp-file + replace pattern config.save_config/zapret.update use
    - a crash mid-write leaves the previous file intact instead of
    truncated."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(text.encode("utf-8", errors="surrogateescape"))
    tmp.replace(path)


def find_steam_path(*, winreg_module=_real_winreg) -> Path | None:
    """Steam's own install directory, from the registry key it writes at
    install time. None if Steam was never installed, or the registry
    points at a folder that no longer exists."""
    try:
        with winreg_module.OpenKey(winreg_module.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            value, _ = winreg_module.QueryValueEx(key, "SteamPath")
    except OSError:
        return None
    if not value:
        return None
    path = Path(value)
    return path if path.is_dir() else None


_LIBRARY_PATH_RE = re.compile(r'"path"\s*"([^"]*)"')


def list_library_folders(steam_path: Path) -> list[Path]:
    """Every Steam library folder - games can live on more than one drive.
    Always includes steam_path itself; libraryfolders.vdf only lists the
    ADDITIONAL ones."""
    folders = [steam_path]
    vdf_path = steam_path / "steamapps" / "libraryfolders.vdf"
    try:
        text = vdf_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return folders
    for match in _LIBRARY_PATH_RE.finditer(text):
        candidate = Path(match.group(1).replace("\\\\", "\\"))
        if candidate.is_dir() and candidate not in folders:
            folders.append(candidate)
    return folders


# Singles that don't say "Jackbox" anywhere in their Steam title - the
# "contains jackbox" check below can never find these on its own. Update
# this list when a new one ships without the word in its name (see the
# design doc's accepted trade-off for going local-only over a live list).
_KNOWN_NON_JACKBOX_SINGLES = frozenset({
    "drawful 2",
    "quiplash",
    "quiplash 2 interlashional",
    "fibbage xl",
})


@dataclass(frozen=True)
class SteamGame:
    appid: str
    name: str


def is_jackbox_title(name: str) -> bool:
    lowered = name.strip().lower()
    return "jackbox" in lowered or lowered in _KNOWN_NON_JACKBOX_SINGLES


_APPID_RE = re.compile(r'"appid"\s*"(\d+)"')
_NAME_RE = re.compile(r'"name"\s*"([^"]*)"')


def scan_installed_jackbox_games(steam_path: Path) -> list[SteamGame]:
    """Every installed title across all libraries whose manifest name
    matches is_jackbox_title - NOT yet filtered by whether it has a
    localconfig.vdf block (see filter_configurable_games in Task 5)."""
    games: list[SteamGame] = []
    for library in list_library_folders(steam_path):
        steamapps = library / "steamapps"
        if not steamapps.is_dir():
            continue
        for manifest in sorted(steamapps.glob("appmanifest_*.acf")):
            try:
                text = manifest.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            appid_match = _APPID_RE.search(text)
            name_match = _NAME_RE.search(text)
            if not appid_match or not name_match:
                continue
            name = name_match.group(1)
            if is_jackbox_title(name):
                games.append(SteamGame(appid=appid_match.group(1), name=name))
    return games


# Task 4: Active Steam account and localconfig.vdf

_USERS_BLOCK_RE = re.compile(r'"(\d+)"\s*\{([^}]*)\}', re.DOTALL)
_TIMESTAMP_RE = re.compile(r'"Timestamp"\s*"(\d+)"')

# The fixed offset between a SteamID64 (what loginusers.vdf keys entries by)
# and the SteamID3 account number (what the userdata/ folder is named after)
# - Valve's own well-documented ID math, not something either file states
# directly.
_STEAM_ID64_BASE = 76561197960265728


def find_active_local_config(steam_path: Path) -> Path | None:
    """The active account's localconfig.vdf, resolved via loginusers.vdf's
    per-account `Timestamp` (the last-login time Steam itself stamps every
    account with, on every login regardless of "remember me"/auto-login
    settings). None if that file is missing or holds no entry with a
    parseable Timestamp at all - see the design doc's "no guessing" rule.

    Originally keyed off a `MostRecent` flag, which turned out not to exist
    in current Steam clients at all - a real install's loginusers.vdf was
    checked (not assumed) and had none. `Timestamp` is present on every
    entry and, unlike `AutoLogin`, never depends on whether "remember my
    password" is even turned on for that account."""
    loginusers = steam_path / "config" / "loginusers.vdf"
    try:
        text = _read_vdf_text(loginusers)
    except OSError:
        return None
    timestamps: list[tuple[int, str]] = []
    for steam_id64, body in _USERS_BLOCK_RE.findall(text):
        match = _TIMESTAMP_RE.search(body)
        if match:
            timestamps.append((int(match.group(1)), steam_id64))
    if not timestamps:
        return None
    _, most_recent_id64 = max(timestamps)
    steam_id3 = str(int(most_recent_id64) - _STEAM_ID64_BASE)
    config_path = steam_path / "userdata" / steam_id3 / "config" / "localconfig.vdf"
    return config_path if config_path.is_file() else None


def filter_configurable_games(config_path: Path, games: list[SteamGame]) -> list[SteamGame]:
    """Keeps only the games that already have an appid block in
    localconfig.vdf - a title Steam has never launched has none yet, and
    this feature does not create one from scratch (see the design doc)."""
    try:
        text = _read_vdf_text(config_path)
    except OSError:
        return []
    return [g for g in games if steam_vdf.read_launch_options(text, g.appid) is not None]


# Task 5: Steam process control

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# How long to wait after `steam.exe -shutdown` before escalating to a
# forced kill - long enough for a normal exit (cloud sync flush, writing
# its own pending config) on a machine under load, short enough that the
# whole operation still feels responsive.
_GRACEFUL_QUIT_TIMEOUT_S = 10.0
_POLL_INTERVAL_S = 0.5


def is_steam_running(*, runner=subprocess.run) -> bool:
    try:
        result = runner(
            ["tasklist", "/FI", "IMAGENAME eq steam.exe"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "steam.exe" in getattr(result, "stdout", "").lower()


def quit_steam(steam_path: Path, *, runner=subprocess.run, sleep=time.sleep) -> bool:
    """Graceful (`steam.exe -shutdown`) first - lets Steam flush its own
    pending state before the file is touched - then a forced kill if it is
    still running after the timeout. Returns whether Steam is confirmed NOT
    running afterwards; callers must never edit the file when this is
    False (see the design doc's error-handling table)."""
    if not is_steam_running(runner=runner):
        return True

    exe = steam_path / "steam.exe"
    try:
        runner([str(exe), "-shutdown"], timeout=10, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("graceful Steam shutdown failed to launch: %s", exc)

    deadline = time.monotonic() + _GRACEFUL_QUIT_TIMEOUT_S
    while time.monotonic() < deadline:
        if not is_steam_running(runner=runner):
            return True
        sleep(_POLL_INTERVAL_S)

    try:
        runner(["taskkill", "/F", "/IM", "steam.exe"], timeout=10, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.error("forced Steam termination failed: %s", exc)
        return False
    return not is_steam_running(runner=runner)


def launch_steam(steam_path: Path, *, popen=subprocess.Popen) -> bool:
    exe = steam_path / "steam.exe"
    try:
        popen([str(exe)], creationflags=_NO_WINDOW)
    except OSError as exc:
        logger.error("could not relaunch Steam: %s", exc)
        return False
    return True


# Task 6: Backup store and apply/revert orchestration

BACKUP_FILE_NAME = "steam_launch_backup.json"


def _backup_path(project_root: Path) -> Path:
    return project_root / BACKUP_FILE_NAME


def load_backups(project_root: Path) -> dict[str, dict]:
    path = _backup_path(project_root)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("could not read %s: %s", path, exc)
        return {}


def save_backups(project_root: Path, data: dict[str, dict]) -> bool:
    path = _backup_path(project_root)
    try:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.error("could not write %s: %s", path, exc)
        return False
    return True


_JBG_FLAG_RE = re.compile(r"-jbg\.config\s+serverUrl=\S*")


def merge_launch_option(existing: str, flag: str) -> str:
    """Replaces an existing -jbg.config serverUrl=... token in place, or
    appends the new flag if there wasn't one - never blows away whatever
    else the user already had in Launch Options."""
    if _JBG_FLAG_RE.search(existing):
        return _JBG_FLAG_RE.sub(flag, existing, count=1)
    return f"{existing} {flag}".strip()


def apply_launch_options(
    steam_path: Path, project_root: Path, appids: list[str], port: int,
    *, runner=subprocess.run, popen=subprocess.Popen, sleep=time.sleep,
) -> dict:
    """Backs up, closes Steam, patches every selected appid, reopens Steam.
    Never touches the file unless the backup write and the Steam-closed
    check both succeeded first."""
    empty = {"ok": False, "error": None, "results": {}, "steamRelaunched": False}
    config_path = find_active_local_config(steam_path)
    if config_path is None:
        return {**empty, "error": "no_active_account"}

    pre_quit_text = _read_vdf_text(config_path)

    backups = load_backups(project_root)
    for appid in appids:
        if appid in backups:
            continue  # never overwrite a real backup with a mid-flight value
        current = steam_vdf.read_launch_options(pre_quit_text, appid)
        if current is not None:
            backups[appid] = {"hadLaunchOptions": True, "value": current}
    if not save_backups(project_root, backups):
        return {**empty, "error": "backup_failed"}

    if not quit_steam(steam_path, runner=runner, sleep=sleep):
        return {**empty, "error": "could_not_close_steam"}

    # Re-read after Steam has actually closed - `-shutdown` can flush
    # pending state to this same file, and editing the pre-quit snapshot
    # would silently discard whatever Steam itself just wrote.
    text = _read_vdf_text(config_path)

    flag = f"-jbg.config serverUrl=127.0.0.1:{port}"
    results: dict[str, dict] = {}
    for appid in appids:
        current = steam_vdf.read_launch_options(text, appid)
        if current is None:
            results[appid] = {"ok": False, "error": "game_not_found"}
            continue
        text = steam_vdf.set_launch_options(text, appid, merge_launch_option(current, flag))
        results[appid] = {"ok": True, "error": None}

    _write_vdf_text(config_path, text)
    relaunched = launch_steam(steam_path, popen=popen)
    return {
        "ok": all(r["ok"] for r in results.values()), "error": None,
        "results": results, "steamRelaunched": relaunched,
    }


def revert_launch_options(
    steam_path: Path, project_root: Path, appids: list[str],
    *, runner=subprocess.run, popen=subprocess.Popen, sleep=time.sleep,
) -> dict:
    empty = {"ok": False, "error": None, "results": {}, "steamRelaunched": False}
    config_path = find_active_local_config(steam_path)
    if config_path is None:
        return {**empty, "error": "no_active_account"}

    if not quit_steam(steam_path, runner=runner, sleep=sleep):
        return {**empty, "error": "could_not_close_steam"}

    backups = load_backups(project_root)
    text = _read_vdf_text(config_path)
    results: dict[str, dict] = {}
    for appid in appids:
        backup = backups.get(appid)
        if backup is None:
            results[appid] = {"ok": False, "error": "no_backup"}
            continue
        try:
            text = steam_vdf.set_launch_options(text, appid, backup["value"])
            results[appid] = {"ok": True, "error": None}
            del backups[appid]
        except steam_vdf.AppBlockNotFound:
            results[appid] = {"ok": False, "error": "game_not_found"}

    _write_vdf_text(config_path, text)
    save_backups(project_root, backups)
    relaunched = launch_steam(steam_path, popen=popen)
    return {
        "ok": all(r["ok"] for r in results.values()), "error": None,
        "results": results, "steamRelaunched": relaunched,
    }
