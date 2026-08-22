"""Check GitHub for a newer BridgeBox release and, if the user asks, replace
the running .exe with it - not zapret's payload (see zapret/update.py), the
app's own version from version.app_version().

Self-update only makes sense for a frozen PyInstaller onefile build: there is
exactly one executable to swap, and sys.executable IS that file while it
runs. In dev mode (`python -m bridgebox.desktop`) running_exe_path() returns
None and callers fall back to opening the release page instead.

The swap itself never touches config.yaml, logs/, certs/ or zapret/ - it only
ever writes next to sys.executable, and only the exe file's own name. Windows
opens a running executable's image with FILE_SHARE_DELETE, so renaming it
(not overwriting it) succeeds even while it is mapped into this very process;
that is the whole trick replace_running_exe relies on. The renamed-away
original cannot be deleted until this process exits, so cleanup_stale_files
is the other half - called once at the start of the NEXT launch, once nothing
holds it anymore.

Two things a self-replacing, admin-elevated binary cannot skip, unlike a
"here's a browser download" flow:

- verify_exe_digest checks the downloaded bytes against the sha256 GitHub
  itself computed at upload time (assets[].digest, added to the Releases API
  in mid-2025). This is not code-signing - it does not vouch for WHO
  published the release - but it does prove the file on disk is exactly the
  file GitHub recorded, which a Content-Length/size check alone does not: a
  connection that gets cut short by exactly the byte count the server
  announced would still "succeed" on a length check, and a corrupted CDN
  edge or a proxy that mangles bytes in transit both slip past HTTPS
  (that only guarantees the channel, not that every byte survived).
- replace_running_exe retries the rename on a locked file (winerror 5/32),
  same class of failure zapret/update.py already retries around, but with
  two sources here instead of one: an antivirus on-write/on-access scan of
  the freshly-downloaded exe (an unsigned, previously-unseen binary is
  exactly what triggers a cloud-lookup scan that can run several seconds,
  not the sub-second case a signed installer gets), AND this process's own
  integrity-check background thread, which can still be mid-hash of the
  OLD exe (integrity.py's WATCHED_GLOBS covers bridgebox.exe) if the update
  is triggered in the first moment after launch - Python's open() on
  Windows does not request FILE_SHARE_DELETE (unlike Rust/Go), so that read
  handle blocks the rename exactly like an antivirus one would. Both are
  self-resolving, neither is fast enough to assume away.

Same shape as zapret/update.py's release-fetching/download half on purpose -
matching call sites read faster - but this module's own allowlist and
constants are separate, because it talks to a different repo.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .winlock import retry_locked as _retry_locked_base

logger = logging.getLogger(__name__)

REPO = "getonjbghelp/bridgebox"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"

# api.github.com/github.com for the release metadata itself; the asset
# download redirects off github.com onto one of the two CDN hosts below -
# same three extra hosts zapret/update.py allows, for the same reason: a
# download that ends up somewhere else is not a BridgeBox release.
ALLOWED_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)

# How a release marks itself as a security-critical update, since GitHub
# Releases has no structured "severity" field. Checked case-insensitively in
# both the release's title and its body - a maintainer publishing from the
# GitHub web UI is more likely to type it in the title, one typed as a tag
# inside a changelog line still needs to be caught.
CRITICAL_MARKER = "[critical]"

_VERSION_RE = re.compile(r"\d+(?:\.\d+)*")

# A onefile build is one executable with everything else bundled inside it -
# generous multiple of what that realistically is, guards against a hostile
# or truncated response rather than a tight fit.
MAX_EXE_BYTES = 300 * 1024 * 1024

# Matches zapret/update.py's own retry budget and reasoning: GitHub's CDN
# drops a connection mid-transfer sometimes, and a dropped connection here has
# cost nothing but time - the file is written to a `.new` stage path and only
# swapped in afterwards.
DOWNLOAD_RETRY_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY_S = 1.0

_STAGE_SUFFIX = ".new"
_BACKUP_SUFFIX = ".old"

# Wider than zapret/update.py's own 5x0.5s budget for its (different) lock
# source: a kernel driver unloading is a bounded, fast OS operation, but a
# cloud-lookup antivirus scan of a brand new, unsigned, never-seen-before
# executable is not - Defender's cloud check alone can run several seconds
# on a slow link. 10x1.5s (~15s worst case) is sized for that, not the
# driver case - both this and the integrity-thread race resolve well within
# it in the common case, so this budget mostly sits unused.
REPLACE_RETRY_ATTEMPTS = 10
REPLACE_RETRY_DELAY_S = 1.5


@dataclass
class AppRelease:
    version: str  # "0.1.2" - leading "v" already stripped, see fetch_latest_release
    name: str
    notes: str  # the release body, Markdown - rendered as-is by the frontend
    html_url: str
    critical: bool
    # The asset a self-update would download. Named for what it is rather
    # than for the .exe it eventually becomes: releases ship the portable
    # .zip, and a bare .exe asset is the exception, not the rule.
    asset_url: str | None = None  # None if the release has nothing usable
    asset_size: int = 0
    asset_digest: str | None = None  # "sha256:<hex>" from GitHub, or None if unavailable
    # True when asset_url points at the portable .zip, so the exe has to be
    # unpacked out of it first - see extract_exe_from_archive.
    asset_is_archive: bool = False


def _numeric_parts(value: str) -> tuple[int, ...]:
    """"v0.1.2" / "0.1.2" / "0.1.2b1" -> (0, 1, 2). Stops at the first
    non-numeric run, which is what drops a PEP 440 pre-release suffix
    (version.app_version() can return "0.1.2b1") the same way
    version.display_version() does - so a clean GitHub tag like "0.1.2"
    compares correctly against the beta build that shipped it."""
    match = _VERSION_RE.match(value.lstrip("vV"))
    if not match:
        return ()
    return tuple(int(part) for part in match.group(0).split("."))


def is_newer(latest: str, installed: str) -> bool:
    """Numeric per-component compare, never lexicographic ("0.1.10" > "0.1.9"
    is exactly the case a string compare gets wrong). Anything unparseable on
    either side is treated as "not newer" - refusing to report an update we
    cannot actually compare is the safe direction for a security prompt."""
    left, right = _numeric_parts(latest), _numeric_parts(installed)
    return bool(left) and bool(right) and left > right


def is_critical(name: str, body: str) -> bool:
    haystack = f"{name or ''} {body or ''}".lower()
    return CRITICAL_MARKER in haystack


async def fetch_latest_release(session, *, url: str = RELEASES_URL) -> AppRelease:
    async with session.get(url, headers={"Accept": "application/vnd.github+json"}) as response:
        response.raise_for_status()
        payload = await response.json()

    tag = str(payload.get("tag_name") or "").strip()
    version = tag[1:] if tag[:1] in ("v", "V") else tag
    name = str(payload.get("name") or tag)
    notes = str(payload.get("body") or "")
    html_url = str(payload.get("html_url") or f"https://github.com/{REPO}/releases/latest")
    _require_allowed_host(html_url)

    chosen = _pick_update_asset(payload.get("assets") or [])
    asset_url: str | None = None
    asset_size = 0
    asset_digest: str | None = None
    asset_is_archive = False
    if chosen is not None:
        candidate = str(chosen.get("browser_download_url") or "")
        _require_allowed_host(candidate)
        asset_url = candidate
        asset_size = int(chosen.get("size") or 0)
        # None on an asset uploaded before GitHub added this (June 2025) -
        # verify_exe_digest treats that as "nothing to check", not an error.
        asset_digest = chosen.get("digest") or None
        asset_is_archive = str(chosen.get("name") or "").lower().endswith(".zip")

    return AppRelease(
        version=version,
        name=name,
        notes=notes,
        html_url=html_url,
        critical=is_critical(name, notes),
        asset_url=asset_url,
        asset_size=asset_size,
        asset_digest=asset_digest,
        asset_is_archive=asset_is_archive,
    )


def _pick_update_asset(assets: list) -> dict | None:
    """The asset a self-update should download, or None if the release
    carries nothing usable.

    A bare .exe wins outright - it needs no unpacking. Otherwise the
    portable .zip, which is what releases actually ship (the exe alone is
    not a runnable BridgeBox: it needs the zapret/ folder beside it, so a
    release publishes the whole folder zipped). Among several .zip assets
    the one whose name says "portable" wins, so a release that also carries
    some other archive does not send the updater after the wrong one."""
    exes = []
    zips = []
    for asset in assets:
        name = str(asset.get("name") or "").lower()
        if name.endswith(".exe"):
            exes.append(asset)
        elif name.endswith(".zip"):
            zips.append(asset)
    if exes:
        return exes[0]
    if not zips:
        return None
    for asset in zips:
        if "portable" in str(asset.get("name") or "").lower():
            return asset
    return zips[0]


def _require_allowed_host(url: str) -> None:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"refusing a non-https release url: {url}")
    if parsed.hostname not in ALLOWED_HOSTS:
        raise ValueError(f"refusing a release url on an unexpected host: {parsed.hostname}")


class IncompleteDownload(RuntimeError):
    """The connection reported success but delivered fewer bytes than the
    server's own Content-Length promised - a truncation that a bare
    "the request didn't raise" check would silently accept. Treated as
    transient (see _is_transient_network_error): it is the same class of
    problem as a dropped connection, just one that happened to end on a
    chunk boundary instead of mid-chunk."""


def _is_transient_network_error(exc: BaseException) -> bool:
    import asyncio

    import aiohttp

    return isinstance(
        exc,
        (
            aiohttp.ServerDisconnectedError,
            aiohttp.ClientConnectionError,
            aiohttp.ClientPayloadError,
            asyncio.TimeoutError,
            IncompleteDownload,
        ),
    )


async def download_exe(
    session,
    url: str,
    dest: Path,
    *,
    max_bytes: int = MAX_EXE_BYTES,
    on_progress=None,
    attempts: int = DOWNLOAD_RETRY_ATTEMPTS,
    delay_s: float = DOWNLOAD_RETRY_DELAY_S,
    sleep=None,
) -> Path:
    """Stream the release's .exe asset to `dest`, aborting past max_bytes.

    Same retry-on-drop shape as zapret/update.py's download_archive: each
    attempt restarts from zero and truncates `dest` rather than resuming,
    because a Range request means trusting a server's byte offsets for a file
    that becomes the next thing this app runs as."""
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
                            raise ValueError(f"exe exceeds {max_bytes} bytes - refusing")
                        handle.write(chunk)
                        if on_progress is not None:
                            on_progress(received, total)
                if total and received != total:
                    # A server that reports its own Content-Length and then
                    # delivers less of it is not "done" - iter_chunked ending
                    # cleanly only means the connection closed, not that the
                    # promised bytes all arrived.
                    raise IncompleteDownload(
                        f"expected {total} bytes, received {received}"
                    )
        except Exception as exc:
            if not _is_transient_network_error(exc) or attempt == attempts:
                dest.unlink(missing_ok=True)
                raise
            logger.warning(
                "download of %s dropped after %d bytes (%s), attempt %d/%d - retrying in %.1fs",
                url, received, exc, attempt, attempts, delay_s,
            )
            await sleep(delay_s)
            continue

        logger.info("downloaded %s (%d bytes) to %s", url, received, dest)
        return dest

    raise RuntimeError("unreachable: the loop above either returns or raises")


EXE_NAME_IN_ARCHIVE = "bridgebox.exe"


def extract_exe_from_archive(
    archive: Path, dest: Path, *, max_bytes: int = MAX_EXE_BYTES
) -> Path:
    """Pull just bridgebox.exe out of a portable release .zip and write it
    to `dest`.

    Searched by file name at any depth, not by a fixed path: the release
    archive nests everything one level down (BridgeBox_Portable-v0.1.2b1/
    bridgebox.exe), and that folder name carries the version, so hard-coding
    it would break on the next release.

    Exactly one member is read and it goes to a path this function was
    handed - the archive's own stored names never steer where anything is
    written, which is what keeps a hostile "../../evil" entry inert
    (unlike ZipFile.extractall, which honours them). The size cap is
    checked against the decompressed stream rather than the entry's own
    declared size, so a zip bomb cannot spend more disk than a real
    download would."""
    archive = Path(archive)
    dest = Path(dest)
    with zipfile.ZipFile(archive) as bundle:
        member = next(
            (
                info
                for info in bundle.infolist()
                if not info.is_dir()
                and PurePosixPath(info.filename).name.lower() == EXE_NAME_IN_ARCHIVE
            ),
            None,
        )
        if member is None:
            raise RuntimeError(
                f"{archive.name} has no {EXE_NAME_IN_ARCHIVE} in it - "
                "not a BridgeBox portable archive"
            )
        dest.parent.mkdir(parents=True, exist_ok=True)
        with bundle.open(member) as source, dest.open("wb") as handle:
            written = 0
            while chunk := source.read(64 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    handle.close()
                    dest.unlink(missing_ok=True)
                    raise ValueError(
                        f"{EXE_NAME_IN_ARCHIVE} in {archive.name} exceeds "
                        f"{max_bytes} bytes - refusing"
                    )
                handle.write(chunk)
    logger.info("extracted %s (%d bytes) from %s", member.filename, written, archive.name)
    return dest


def _sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def verify_exe_digest(path: Path, digest: str | None) -> None:
    """Check `path` against the sha256 GitHub itself computed when the
    release asset was uploaded (AppRelease.exe_digest, "sha256:<hex>" or
    None - see fetch_latest_release). Raises ValueError on a mismatch;
    callers must not swap the exe in when this raises.

    Not code-signing - a compromised release pipeline could upload a bad exe
    with a self-consistent digest just as easily as a good one. What this
    catches is everything AFTER upload: a connection that ends early inside
    its own promised byte count, a corrupted CDN edge, a proxy that mangles
    bytes in transit - none of which raise on their own and none of which
    HTTPS (a channel guarantee, not a bytes-on-disk guarantee) rules out.

    A release with no digest - uploaded before GitHub added the field in
    June 2025 - is logged and let through rather than blocked forever."""
    if digest is None:
        logger.warning("no digest to verify %s against - proceeding without one", path)
        return
    algo, _, expected = digest.partition(":")
    if algo != "sha256" or not expected:
        raise ValueError(f"unrecognised digest format from GitHub: {digest!r}")
    actual = _sha256_file(path)
    if actual.lower() != expected.lower():
        raise ValueError(
            f"downloaded exe does not match its published checksum "
            f"(expected {expected}, got {actual}) - refusing to install it"
        )


def running_exe_path() -> Path | None:
    """sys.executable while frozen IS the onefile .exe on disk - the thing
    self-update has to replace. None in dev mode: there is no single file to
    swap, so callers fall back to sending the user to the release page."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable)


def _retry_locked(op, *, sleep=time.sleep):
    """A freshly-written exe commonly gets a brief antivirus on-write or
    on-access scan, which holds exactly the kind of handle this retries
    around - see winlock.retry_locked for the retry loop itself, shared
    with zapret/update.py's own (unrelated-cause, same-winerrors) lock case."""
    return _retry_locked_base(
        op, what="exe swap step",
        attempts=REPLACE_RETRY_ATTEMPTS, delay_s=REPLACE_RETRY_DELAY_S, sleep=sleep,
    )


def replace_running_exe(new_path: Path, current_path: Path, *, sleep=time.sleep) -> Path:
    """Swap `current_path` (this process's own running image) for `new_path`.

    Windows opens the loaded executable with FILE_SHARE_DELETE, so renaming -
    not overwriting - the running exe succeeds even while it is mapped: the
    open handle keeps the old bytes alive under the new (backup) name until
    this process exits. Move the running exe aside, put the new one at the
    real name, done - no helper process, no waiting for a lock to clear
    itself in the common case, and a bounded retry (_retry_locked) for the
    uncommon one.

    Returns the backup path. On any failure after the first rename, rolls
    `current_path` back so the existing install still launches next time."""
    backup = current_path.with_name(current_path.name + _BACKUP_SUFFIX)
    backup.unlink(missing_ok=True)  # leftover from an update never cleaned up
    _retry_locked(lambda: os.replace(current_path, backup), sleep=sleep)
    try:
        _retry_locked(lambda: os.replace(new_path, current_path), sleep=sleep)
    except OSError:
        _retry_locked(lambda: os.replace(backup, current_path), sleep=sleep)
        raise
    return backup


def cleanup_stale_files(current_path: Path) -> None:
    """Best-effort: delete a `.old` backup or `.new` stage file a previous
    self-update left behind - the `.old` from a completed swap (see
    replace_running_exe), the `.new` from one that downloaded but was never
    finished (interrupted, or the digest check refused it - see
    verify_exe_digest).

    Called once at startup. By the time this (new) process is running,
    whatever briefly held `.old` locked (the exited old process itself, or
    an antivirus scan) is done with it - but stays best-effort, since a
    leftover file is harmless and the next launch will try again."""
    for suffix in (_BACKUP_SUFFIX, _STAGE_SUFFIX):
        stale = current_path.with_name(current_path.name + suffix)
        try:
            stale.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("could not remove leftover %s - will retry next launch", stale,
                            exc_info=exc)


def stage_path_for(current_path: Path) -> Path:
    return current_path.with_name(current_path.name + _STAGE_SUFFIX)
