"""Update the vendored zapret payload from a Flowseal release.

Binaries - winws.exe, its DLLs, WinDivert and the .bin payloads - are replaced
outright. Strategies go through strategy_adapt: the Flowseal originals depend
on service.bat, a bin/ subfolder and a dozen unvendored assets (see
zapret/README.md), so they are parsed into a token allowlist and re-rendered
into BridgeBox's own template, never copied.

install_release is the whole post-download flow and the only entry point
desktop.Api._update_coro needs. Its ordering is load-bearing twice over:
binaries before strategies, and plan-everything before writing anything.

A file BridgeBox generated carries a self-verifying stamp on its last line
(strategy_adapt.stamp), and only such a file is ever overwritten. Anything
else - including all 21 hand-adapted strategies shipped today - is treated as
the user's and gets a "<Name> (updated).bat" sibling instead. Nothing is ever
deleted or renamed, so config.zapret.strategy cannot be left dangling.

Every function is either pure or takes an injected aiohttp session, matching
the DI style of RuntimeCore/ZapretProcess/RoomsProxy, so the whole flow is
testable without a network or a real archive.
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .. import i18n
from ..winlock import retry_locked as _retry_locked_base
from .strategy_adapt import adapt_strategy, is_unmodified_generated, stamp

logger = logging.getLogger(__name__)

# Pinned, never user-configurable: this is where an executable that later runs
# as Administrator comes from, so it is a code-execution source, not a
# preference. Same reasoning as RewriteConfig's https-only validator, one
# level more serious.
REPO = "Flowseal/zapret-discord-youtube"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

# Redirects out of api.github.com land on the CDN; anything else means we are
# not talking to who we think we are.
ALLOWED_HOSTS = frozenset(
    {"api.github.com", "github.com", "objects.githubusercontent.com",
     "release-assets.githubusercontent.com"}
)

# What we take out of the archive, by BASENAME. The archive's own paths are
# never used - see extract_allowed.
BINARY_NAMES = frozenset({"winws.exe", "cygwin1.dll", "WinDivert.dll", "WinDivert64.sys"})

# Guards against a hostile or corrupt archive. The real 1.10.0 zip is ~1.5 MB
# and holds a few dozen files; these are generous multiples, not tight fits.
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_MEMBERS = 2000

_VERSION_RE = re.compile(r'zapretver\s*=\s*"([^"]+)"')
_VERSION_PART = re.compile(r"\d+")

# ponytail: fixed linear backoff, ~9s worst case. The real quantity here is how
# long the WinDivert service takes to unload WinDivert64.sys after the last
# winws.exe exits, and that is not knowable from inside this process - the
# driver unloads asynchronously and nothing signals it. These are therefore a
# calibration knob, not a measurement. If field logs show the budget genuinely
# exhausting on WinDivert64.sys, the next escalation is one `sc stop WinDivert`
# between the final two attempts - gate that on evidence, because `sc delete`
# on a loaded driver leaves it pending-delete, which makes the file MORE locked
# until a reboot, and the service name is version-dependent besides.
LOCK_RETRY_ATTEMPTS = 5
LOCK_RETRY_DELAY_S = 0.5

# The download is the other half that kept failing, with ServerDisconnectedError
# rather than WinError 5: GitHub's CDN drops a connection mid-transfer, and one
# dropped connection killed the whole update. A retry is the right answer -
# the file is fetched into a temp folder and only moved into place afterwards,
# so a failed attempt has cost nothing but time.
DOWNLOAD_RETRY_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY_S = 1.0


class ApplyFailed(RuntimeError):
    """apply_update could not finish.

    `unrestored` is the part that decides what the user has to do:
      - empty    -> every file was put back, the install is exactly as it was,
                    and retrying the update is safe.
      - non-empty -> those targets are missing and their .bak siblings are
                    still on disk, so zapret is half-updated and needs the
                    backups restored (or a reinstall) before it will run.
    """

    def __init__(
        self, applied: list[str], unrestored: list[str], cause: BaseException, lang: str = "ru"
    ):
        self.applied = applied
        self.unrestored = unrestored
        self.cause = cause
        if unrestored:
            detail = i18n.t("update.apply_failed_partial", lang, files=", ".join(unrestored))
        else:
            detail = i18n.t("update.apply_failed_rolled_back", lang)
        super().__init__(i18n.t("update.apply_failed_cause", lang, detail=detail, cause=cause))


# Well-known SIDs, not "Administrators": icacls resolves account names in the
# machine's own language, so the English name fails outright on a Russian
# Windows. Same reasoning, and the same two constants, as tls/ca.py.
SID_SYSTEM = "S-1-5-18"
SID_ADMINISTRATORS = "S-1-5-32-544"

ICACLS_TIMEOUT_S = 20

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# MoveFileExW's "do it on the next boot" flag. Passing NULL as the destination
# with this set is the documented way to schedule a delete-on-reboot.
_MOVEFILE_DELAY_UNTIL_REBOOT = 0x4


def grant_full_control(directory: Path, *, runner=subprocess.run) -> bool:
    """Make sure Administrators and SYSTEM can rewrite everything in `directory`.

    The second half of the WinError 5 story. A denied rename is usually a live
    handle - which is what _retry_locked and the winws sweep address - but it
    can also be a genuinely wrong ACL: zapret/ is unpacked by whatever ran
    first, and a folder restored from a backup or copied off another machine
    routinely arrives with inherited permissions that this process, elevated or
    not, cannot write through.

    Best-effort and never raises: a refused icacls must not turn an update that
    would have worked into a failure."""
    try:
        result = runner(
            [
                "icacls",
                str(directory),
                "/grant",
                f"*{SID_ADMINISTRATORS}:(OI)(CI)F",
                f"*{SID_SYSTEM}:(OI)(CI)F",
                "/T",  # the payload files are what need writing, not the folder
                "/C",  # keep going past a file that is currently locked
            ],
            capture_output=True,
            timeout=ICACLS_TIMEOUT_S,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("could not grant write access to %s: %s", directory, exc)
        return False
    ok = getattr(result, "returncode", 1) == 0
    if not ok:
        logger.warning(
            "icacls did not fully succeed on %s (rc=%s) - continuing anyway",
            directory,
            getattr(result, "returncode", "?"),
        )
    return ok


def delete_on_reboot(path: Path) -> bool:
    """Queue `path` for deletion at the next boot.

    The last resort for a .bak that nothing will let go of. Without it, a
    successful update could still end in a locked leftover: the old code
    unlinked the backups outside its try block, so one stuck file turned a
    finished update into a raised exception and a scary message.

    Windows-only by construction; anywhere else this simply reports False."""
    try:
        import ctypes

        ok = bool(
            ctypes.windll.kernel32.MoveFileExW(  # type: ignore[attr-defined]
                str(path), None, _MOVEFILE_DELAY_UNTIL_REBOOT
            )
        )
    except (AttributeError, OSError) as exc:
        logger.warning("could not schedule %s for deletion on reboot: %s", path, exc)
        return False
    if ok:
        logger.info("%s is locked - scheduled for deletion on the next reboot", path)
    return ok


def _retry_locked(
    op,
    *,
    what: str,
    attempts: int = LOCK_RETRY_ATTEMPTS,
    delay_s: float = LOCK_RETRY_DELAY_S,
    sleep=time.sleep,
):
    """WinDivert64.sys is a kernel driver, so `winws.exe` exiting does NOT
    release it - the service unloads it a moment later, asynchronously, and
    until then every replace() on it fails with WinError 5. Waiting is the
    whole fix - see winlock.retry_locked for the retry loop itself, shared
    with app_update.py's own (unrelated-cause, same-winerrors) lock case."""
    return _retry_locked_base(op, what=what, attempts=attempts, delay_s=delay_s, sleep=sleep)


@dataclass(frozen=True)
class Release:
    version: str
    zip_url: str
    zip_size: int


def read_installed_version(zapret_dir: Path) -> str | None:
    """Parse ver.installed.txt, the provenance stamp Flowseal ships.

    The file is TOML-ish (`zapretver = "1.10.0"`) but nothing else in the app
    reads it, so a regex beats taking a TOML dependency for two lines."""
    path = Path(zapret_dir) / "ver.installed.txt"
    if not path.exists():
        return None
    match = _VERSION_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    return match.group(1).strip() if match else None


def is_newer(latest: str, installed: str | None) -> bool:
    """Compare two Flowseal version stamps.

    Numeric per-component, not lexicographic: "1.10.0" > "1.9.9" is the whole
    reason this is not a string compare. Anything unparseable is treated as
    "not newer" - refusing to update on a version we do not understand is the
    safe direction when the payload runs as Administrator."""
    if not latest or installed is None:
        return bool(latest) and installed is None

    def parts(value: str) -> tuple[int, ...]:
        try:
            return tuple(int(p) for p in _VERSION_PART.findall(value))
        except ValueError:
            # Same cap and same fix as app_update._numeric_parts: CPython
            # refuses int(str) past 4300 digits, and a Flowseal tag_name is
            # unbounded free-form text - treat it as unparseable rather than
            # letting the update check crash.
            return ()

    left, right = parts(latest), parts(installed)
    return bool(left) and bool(right) and left > right


async def fetch_latest_release(session, *, url: str = RELEASES_URL) -> Release:
    """Ask GitHub for the newest release and pick its .zip asset.

    The .zip rather than the .rar or .tar.gz: stdlib zipfile reads it, and the
    other two would each cost a dependency for no gain."""
    async with session.get(url, headers={"Accept": "application/vnd.github+json"}) as response:
        response.raise_for_status()
        payload = await response.json()

    version = str(payload.get("tag_name") or "").strip()
    for asset in payload.get("assets") or []:
        name = str(asset.get("name") or "")
        if name.lower().endswith(".zip"):
            url = str(asset.get("browser_download_url") or "")
            _require_allowed_host(url)
            return Release(version=version, zip_url=url, zip_size=int(asset.get("size") or 0))

    raise ValueError(f"release {version or '?'} has no .zip asset")


def _require_allowed_host(url: str) -> None:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"refusing a non-https download url: {url}")
    if parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"refusing a download from an unexpected host: {parsed.hostname}")


def _is_transient_network_error(exc: BaseException) -> bool:
    """Worth another attempt, as opposed to worth reporting.

    ServerDisconnectedError is the one actually seen in the field: GitHub's CDN
    closes the connection part-way through the transfer. Timeouts and the
    generic connection errors belong in the same class. ValueError (our own
    size/host guards) and HTTP 4xx deliberately do not - those will fail
    identically every time."""
    import asyncio

    import aiohttp

    return isinstance(
        exc,
        (
            aiohttp.ServerDisconnectedError,
            aiohttp.ClientConnectionError,
            aiohttp.ClientPayloadError,
            asyncio.TimeoutError,
        ),
    )


async def download_archive(
    session,
    url: str,
    dest: Path,
    *,
    max_bytes: int = MAX_ARCHIVE_BYTES,
    on_progress=None,
    attempts: int = DOWNLOAD_RETRY_ATTEMPTS,
    delay_s: float = DOWNLOAD_RETRY_DELAY_S,
    sleep=None,
) -> Path:
    """Stream the release zip to `dest`, aborting if it exceeds max_bytes.

    Streamed rather than read() in one go so the size cap can stop a hostile
    response before it is all in memory, and so the UI can show progress.

    Retried on a dropped connection. Each attempt restarts from zero and
    truncates `dest` - resuming with a Range request would be the efficient
    thing, but it also means trusting a server's byte offsets for a file that
    later runs as Administrator, and the archive is ~1.5 MB."""
    import asyncio

    _require_allowed_host(url)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    sleep = sleep if sleep is not None else asyncio.sleep

    for attempt in range(1, attempts + 1):
        received = 0
        try:
            async with session.get(url) as response:
                response.raise_for_status()
                _require_allowed_host(str(response.url))
                total = int(response.headers.get("Content-Length") or 0)
                with dest.open("wb") as handle:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        received += len(chunk)
                        if received > max_bytes:
                            handle.close()
                            dest.unlink(missing_ok=True)
                            raise ValueError(f"archive exceeds {max_bytes} bytes - refusing")
                        handle.write(chunk)
                        if on_progress is not None:
                            on_progress(received, total)
        except Exception as exc:
            if not _is_transient_network_error(exc) or attempt == attempts:
                dest.unlink(missing_ok=True)
                raise
            logger.warning(
                "download of %s dropped after %d bytes (%s), attempt %d/%d - retrying in %.1fs",
                url,
                received,
                exc,
                attempt,
                attempts,
                delay_s,
            )
            await sleep(delay_s)
            continue

        logger.info("downloaded %s (%d bytes) to %s", url, received, dest)
        return dest

    raise RuntimeError("unreachable: the loop above either returns or raises")


def _is_wanted(name: str) -> bool:
    return name in BINARY_NAMES or name.endswith(".bin")


def extract_allowed(zip_path: Path, dest: Path) -> dict[str, Path]:
    """Pull the files we want out of the archive into `dest`, flat.

    Zip-slip is removed by construction rather than by sanitising: only the
    member's BASENAME is used, and the destination path is built here from a
    directory we chose. The archive's own path never reaches the filesystem,
    so `../../windows/system32/x.dll` simply extracts as `x.dll` - and is then
    rejected anyway for not being on the allowlist.

    Returns {basename: written path}."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    budget = MAX_ARCHIVE_BYTES

    with zipfile.ZipFile(zip_path) as archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS:
            raise ValueError(f"archive holds {len(members)} members - refusing")

        for info in members:
            if info.is_dir():
                continue
            name = PurePosixPath(info.filename.replace("\\", "/")).name
            if not name or not _is_wanted(name):
                continue
            if info.file_size > MAX_MEMBER_BYTES:
                raise ValueError(f"{name} is {info.file_size} bytes - refusing")
            budget -= info.file_size
            if budget < 0:
                raise ValueError("archive expands past the size budget - refusing")

            target = dest / name
            with archive.open(info) as source, target.open("wb") as handle:
                handle.write(source.read())
            written[name] = target

    if "winws.exe" not in written:
        raise ValueError("archive contains no winws.exe - not a zapret release")
    if written["winws.exe"].read_bytes()[:2] != b"MZ":
        # Cheap sanity check on the one file that gets executed as
        # Administrator: a PE image starts with MZ. Not a signature check -
        # GitHub publishes no checksums for these assets, so https plus the
        # pinned repo is the whole trust story (stated plainly, not implied).
        raise ValueError("winws.exe is not a PE executable - refusing")

    logger.info("extracted %d files from %s", len(written), zip_path)
    return written


# "%BIN%name.bin" -> name.bin, the same reference form test_strategy_assets
# checks. The .bat files set BIN to the zapret root.
_BIN_REF = re.compile(r"%BIN%([^\"^\s]+)")


def referenced_payloads(strategies_dir: Path) -> set[str]:
    """Asset filenames the adapted strategies actually name."""
    names: set[str] = set()
    for bat in sorted(Path(strategies_dir).glob("*.bat")):
        names.update(_BIN_REF.findall(bat.read_text(encoding="utf-8", errors="replace")))
    return names


def select_for_install(
    staged: dict[str, Path], strategies_dir: Path, *, extra_refs: set[str] = frozenset()
) -> dict[str, Path]:
    """Narrow a staged extraction to what this install actually runs.

    A Flowseal release carries payloads for the seven profiles BridgeBox never
    ported - Discord voice, Steam, Tencent, the game port ranges. Copying them
    would leave files on disk that no strategy references, which is precisely
    the state test_every_payload_file_is_used_by_some_strategy exists to
    forbid: that invariant is how the nine substituted fake-ClientHello
    payloads were finally caught, so it is worth keeping true by construction
    rather than relaxing to let an updater past it.

    `extra_refs` is the other half of that invariant: strategies already on
    disk are not the whole story when the same update is also writing new ones,
    and a brand-new strategy can legitimately name a .bin nothing existing yet
    references. install_release passes the payload refs of its StrategyPlan."""
    wanted = referenced_payloads(strategies_dir) | set(extra_refs)
    return {
        name: path
        for name, path in staged.items()
        if name in BINARY_NAMES or name in wanted
    }


# Top-level only, by NAME AND POSITION (unlike extract_allowed's basename-only
# matching): a nested strategies/general.bat must not shadow a real top-level
# original from elsewhere in the release.
_ORIGINAL_STRATEGY_RE = re.compile(r"^general.*\.bat$", re.IGNORECASE)


def extract_original_strategies(zip_path: Path, dest: Path) -> dict[str, Path]:
    """Pull raw Flowseal general*.bat originals out of the archive - the
    material strategy_adapt.adapt_strategy works from. These are never
    executed or copied into strategies/ as-is; they only ever feed the
    adapter, same trust boundary as extract_allowed one step further down."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    budget = MAX_ARCHIVE_BYTES

    with zipfile.ZipFile(zip_path) as archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS:
            raise ValueError(f"archive holds {len(members)} members - refusing")

        file_parts = [
            PurePosixPath(info.filename.replace("\\", "/")).parts
            for info in members
            if not info.is_dir()
        ]
        # GitHub packages a release's assets as "reponame-version/..." - every
        # file one level deeper than it looks, which made every general*.bat
        # in 1.10.2's zip fail the top-level check below and this function
        # return nothing at all (confirmed against a real download: 0 added,
        # 0 skipped - not even logged as a miss). Only stripped when literally
        # every file shares that one root, so the nested-decoy protection this
        # function exists for (see the class comment on _ORIGINAL_STRATEGY_RE)
        # still applies to a real top-level zip with a genuine subfolder in it.
        roots = {parts[0] for parts in file_parts if parts}
        # Both conditions matter: a single shared root is also what a zip with
        # exactly one true top-level file looks like (that file's own name IS
        # the "root"), and stripping there would eat the filename itself.
        # Only a root every file sits BELOW qualifies as a wrapper folder.
        strip_root = len(roots) == 1 and all(len(parts) > 1 for parts in file_parts)

        for info in members:
            if info.is_dir():
                continue
            parts = PurePosixPath(info.filename.replace("\\", "/")).parts
            if strip_root:
                parts = parts[1:]
            if len(parts) != 1 or not _ORIGINAL_STRATEGY_RE.match(parts[0]):
                continue
            name = parts[0]
            if info.file_size > MAX_MEMBER_BYTES:
                raise ValueError(f"{name} is {info.file_size} bytes - refusing")
            budget -= info.file_size
            if budget < 0:
                raise ValueError("archive expands past the size budget - refusing")

            target = dest / name
            with archive.open(info) as source, target.open("wb") as handle:
                handle.write(source.read())
            written[name] = target

    logger.info("extracted %d original strategy files from %s", len(written), zip_path)
    return written


# The suffix an updated adaptation gets when the file it would replace is the
# user's. Deliberately version-independent: the fork is itself stamped, so the
# NEXT release rewrites it in place. One extra file per strategy forever,
# instead of one per release piling up.
FORK_SUFFIX = " (updated)"


@dataclass(frozen=True)
class StrategyPlan:
    """What an update would do to strategies/, decided before anything is
    written. Every list is reported to the user - see SettingsScreen."""

    write: dict[str, str]  # filename -> stamped .bat text
    added: list[str]
    updated: list[str]
    forked: list[tuple[str, str]]  # (original left untouched, fork written)
    skipped: list[tuple[str, str]]  # (original name, reason)


def plan_strategies(
    staged_original_bats: dict[str, Path],
    strategies_dir: Path,
    available_bins: set[str],
    version: str,
) -> StrategyPlan:
    """Decide what to do with every general*.bat in a release.

    Adapts ALL of them, not just qualifiers BridgeBox has never seen. That is
    the point: a release whose *changed parameters* land on a strategy already
    on disk used to produce literally nothing, because the old
    stage_new_strategies skipped anything whose target filename existed.

    Never overwrites a file BridgeBox did not itself generate. The test is
    strategy_adapt.is_unmodified_generated - a self-verifying stamp on the last
    line - and everything unstamped counts as the user's, which today means all
    21 hand-adapted files that ship with the app. Those get a "<Name>
    (updated).bat" sibling instead, leaving the original byte-identical.

    Nothing is ever deleted or renamed, so config.zapret.strategy cannot be
    left pointing at a file that no longer exists."""
    strategies_dir = Path(strategies_dir)
    write: dict[str, str] = {}
    added: list[str] = []
    updated: list[str] = []
    forked: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []

    def read(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    for name, path in sorted(staged_original_bats.items()):
        result = adapt_strategy(
            name, path.read_text(encoding="utf-8", errors="replace"), available_bins=available_bins
        )
        if not result.ok:
            skipped.append((name, result.reason))
            logger.warning("skipped adapting %s: %s", name, result.reason)
            continue

        content = stamp(result.content, version)
        target = strategies_dir / result.filename
        existing = read(target) if target.exists() else None

        if existing is None:
            write[result.filename] = content
            added.append(result.filename)
        elif existing == content:
            continue  # the release did not change this one - no churn
        elif is_unmodified_generated(existing):
            write[result.filename] = content
            updated.append(result.filename)
        else:
            fork_name = f"{result.filename[:-4]}{FORK_SUFFIX}.bat"
            fork_target = strategies_dir / fork_name
            fork_existing = read(fork_target) if fork_target.exists() else None
            if fork_existing is not None and not is_unmodified_generated(fork_existing):
                skipped.append(
                    (name, f"и {result.filename}, и {fork_name} изменены вручную")
                )
                logger.warning("skipped %s: both it and its fork are user-modified", name)
                continue
            if fork_existing == content:
                continue
            write[fork_name] = content
            forked.append((result.filename, fork_name))

    logger.info(
        "strategy plan for %s: %d added, %d updated, %d forked, %d skipped",
        version,
        len(added),
        len(updated),
        len(forked),
        len(skipped),
    )
    return StrategyPlan(write, added, updated, forked, skipped)


def apply_update(
    staged: dict[str, Path], zapret_dir: Path, *, sleep=time.sleep, lang: str = "ru"
) -> list[str]:
    """Move the staged files into zapret/, with rollback on any failure.

    Caller must have stopped zapret first - including any winws.exe this
    session did not start, via process.kill_all_winws(). Even then the files
    can still be locked for a moment: WinDivert64.sys is a kernel driver whose
    service unloads it asynchronously, so every replace here is wrapped in
    _retry_locked rather than attempted once.

    Raises ApplyFailed, which distinguishes "rolled back cleanly, retrying is
    safe" from "half applied, .bak files are still on disk"."""
    zapret_dir = Path(zapret_dir)
    backups: dict[Path, Path] = {}
    applied: list[str] = []

    try:
        for name, source in sorted(staged.items()):
            target = zapret_dir / name
            if target.exists():
                backup = target.with_suffix(target.suffix + ".bak")
                _retry_locked(lambda: target.replace(backup), what=target.name, sleep=sleep)
                backups[target] = backup
            # Same atomic pattern strategies.save_hostlist uses: write a
            # sibling, then rename, so a crash cannot leave a truncated file.
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_bytes(source.read_bytes())
            _retry_locked(lambda: tmp.replace(target), what=target.name, sleep=sleep)
            applied.append(name)
    except Exception as exc:
        logger.exception("update failed after applying %s - rolling back", applied)
        # Each restore is guarded on its own. A single still-locked file used
        # to abort the whole rollback, leaving the rest of the install in the
        # new state; worse, the restore's own exception propagated out of this
        # handler and DISCARDED the real cause, so the log named the wrong
        # problem. Now every file gets its turn and the cause always survives.
        unrestored: list[str] = []
        for target, backup in backups.items():
            try:
                target.unlink(missing_ok=True)
                _retry_locked(lambda: backup.replace(target), what=target.name, sleep=sleep)
            except Exception:
                # The .bak is deliberately left in place - it is the only copy
                # of the original file left, and ApplyFailed's message tells
                # the user it is there.
                logger.exception("could not restore %s from %s", target, backup)
                unrestored.append(target.name)
        raise ApplyFailed(applied, unrestored, exc, lang) from exc

    # Guarded, one by one. This used to be a bare unlink outside the try: a
    # single .bak that something still held raised out of a COMPLETED update,
    # so a success was reported to the user as a failure. The new file is
    # already in place by now - a leftover backup is untidy, never broken.
    for backup in backups.values():
        try:
            backup.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("could not remove the backup %s (%s)", backup, exc)
            delete_on_reboot(backup)
    logger.info("applied %d updated files to %s", len(applied), zapret_dir)
    return applied


def install_release(
    archive: Path,
    *,
    zapret_dir: Path,
    strategies_dir: Path,
    stage_dir: Path,
    version: str,
    sleep=time.sleep,
    lang: str = "ru",
) -> tuple[list[str], StrategyPlan]:
    """Everything after the download: extract, plan, apply, restamp.

    Lifted out of desktop.Api._update_coro so the ordering below can be pinned
    by a test - it cannot be, inside a pywebview Api method.

    Binaries land BEFORE strategies, deliberately. If the strategy write fails
    you are left with new binaries and old strategies, which runs; the reverse
    would leave a strategy naming a .bin that never arrived."""
    zapret_dir, strategies_dir, stage_dir = Path(zapret_dir), Path(strategies_dir), Path(stage_dir)

    # Before anything is renamed. A wrong ACL and a live handle both surface as
    # WinError 5, and only one of them is fixed by waiting - so the update
    # stops guessing which it is and rules the ACL out first.
    grant_full_control(zapret_dir)

    staged = extract_allowed(archive, stage_dir / "staged")
    originals = extract_original_strategies(archive, stage_dir / "originals")

    # What a new strategy is allowed to reference: payloads this release ships,
    # plus the ones already installed (a release need not re-ship every .bin).
    available_bins = {name for name in staged if name.endswith(".bin")}
    available_bins |= {p.name for p in zapret_dir.glob("*.bin")}
    plan = plan_strategies(originals, strategies_dir, available_bins, version)

    new_refs: set[str] = set()
    for content in plan.write.values():
        new_refs.update(_BIN_REF.findall(content))

    selected = select_for_install(staged, strategies_dir, extra_refs=new_refs)
    applied = apply_update(selected, zapret_dir, sleep=sleep, lang=lang)

    if plan.write:
        # Materialise the rendered text, then reuse apply_update rather than
        # writing a second writer: backups, rollback and the locked-file retry
        # all come for free. `.bat.bak`/`.bat.tmp` siblings do not match
        # discover_strategies' glob("*.bat"), so a crash mid-apply cannot
        # surface a half-written file in the strategy dropdown.
        pending = stage_dir / "strategies"
        pending.mkdir(parents=True, exist_ok=True)
        rendered = {}
        for filename, content in plan.write.items():
            path = pending / filename
            path.write_text(content, encoding="utf-8")
            rendered[filename] = path
        apply_update(rendered, strategies_dir, sleep=sleep, lang=lang)

    write_installed_version(zapret_dir, version)
    return applied, plan


def write_installed_version(zapret_dir: Path, version: str) -> None:
    """Restamp ver.installed.txt so the next check compares against what is
    actually on disk."""
    path = Path(zapret_dir) / "ver.installed.txt"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(f'zapretver = "{version}"\nauthor = "Flowseal"\n', encoding="utf-8")
    tmp.replace(path)
