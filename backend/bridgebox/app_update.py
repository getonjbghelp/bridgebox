"""Check GitHub for a newer BridgeBox release and, if the user asks, replace
the running install with it - not zapret's payload (see zapret/update.py),
the app's own version from version.app_version().

Self-update only makes sense for a frozen PyInstaller onedir build: there is
exactly one exe and one _internal/ folder beside it to swap, and
sys.executable IS that exe while it runs (running_exe_path() /
running_internal_dir()). In dev mode (`python -m bridgebox.desktop`) both
return None and callers fall back to opening the release page instead.
_internal/ is not optional cargo - it is the Python runtime, every compiled
extension, and frontend/dist, i.e. everything the exe stub actually needs to
run at all; swapping the exe alone and leaving the OLD _internal/ behind
would start a new bridgebox.exe against a mismatched runtime, so both move
together or neither does.

The swap itself never touches config.yaml, logs/, certs/ or zapret/ - it only
ever writes bridgebox.exe and _internal/ next to sys.executable.

WHY THE SWAP DOES NOT HAPPEN IN THIS PROCESS
---------------------------------------------
An earlier version of this module tried to rename bridgebox.exe and
_internal/ in place while still running, on the assumption that Windows opens
a running executable's image (and a loaded DLL inside _internal/) with
FILE_SHARE_DELETE. That is true of the exe's own process image, but not
reliably true of the DLLs and compiled extensions _internal/ holds, which get
LoadLibrary'd by the Python runtime and stay mapped for the process's whole
life - and a rename Windows sees as touching any handle without
FILE_SHARE_DELETE fails with WinError 5, indistinguishable on the surface
from the ACL and antivirus-scan causes below. Confirmed against a real
failure (a user's log showing exactly this rename failing, every retry, with
nothing else running that could hold it) and against PyInstaller's own
maintainers describing the identical limitation for onedir self-updaters:
"you cannot replace or rename the _internal folder while the application is
actively running... [use] a launcher/updater process separate from the main
application" (pyinstaller/pyinstaller#9263). A second bridgebox.exe process
cannot be that launcher either - it would LoadLibrary the very same
_internal/ DLLs into itself and hit the identical lock - so the actual swap
now happens from a plain batch script (build_relaunch_script) that never
touches _internal/ at all, spawned detached right before this process exits
and waits for it to be gone before touching anything. See
desktop.Api.restart_after_app_update for where that is triggered, and
build_relaunch_script's own docstring for the script itself.

This module's own job stopped at staging: download, verify, and extract into
`.new` paths beside the real ones (stage_path_for) - none of which touches
the live install, so all of it is still safe to do while running. The
renamed-away originals the relaunch script produces cannot be deleted until
the NEW process is running (nothing holds them by then), so
cleanup_stale_files is the other half - called once at the start of the NEXT
launch.

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
- The relaunch script grants Administrators/SYSTEM full control on both
  paths before swapping (a portable install lives wherever the user unzipped
  it, not somewhere this app ever set permissions on) and retries each
  rename briefly - an antivirus on-write/on-access scan of the
  freshly-staged files is a real, self-resolving cause of the same
  WinError 5, distinct from the process-lifetime lock above and NOT solved
  by moving the swap out of this process.

Same shape as zapret/update.py's release-fetching/download half on purpose -
matching call sites read faster - but this module's own allowlist and
constants are separate, because it talks to a different repo.
"""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .zapret.update import SID_ADMINISTRATORS, SID_SYSTEM

logger = logging.getLogger(__name__)

REPO = "getonjbghelp/bridgebox"
RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
ALL_RELEASES_URL = f"https://api.github.com/repos/{REPO}/releases?per_page=100"

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

# The compressed portable .zip a release ships - generous multiple of what
# that realistically is, guards against a hostile or truncated response
# rather than a tight fit.
MAX_EXE_BYTES = 300 * 1024 * 1024

# The DECOMPRESSED total of bridgebox.exe + _internal/ extracted from that
# zip - bigger than MAX_EXE_BYTES on purpose, since _internal/ (the Python
# runtime, every compiled extension, frontend/dist) is several times the
# size of the zip that compresses it. Checked against the actual bytes
# written, not any entry's declared size, so a zip bomb cannot spend more
# disk than a real release would - see extract_release_from_archive.
MAX_EXTRACTED_BYTES = 500 * 1024 * 1024

# Matches zapret/update.py's own retry budget and reasoning: GitHub's CDN
# drops a connection mid-transfer sometimes, and a dropped connection here has
# cost nothing but time - the file is written to a `.new` stage path and only
# swapped in afterwards.
DOWNLOAD_RETRY_ATTEMPTS = 3
DOWNLOAD_RETRY_DELAY_S = 1.0

_STAGE_SUFFIX = ".new"
_BACKUP_SUFFIX = ".old"

# How long the relaunch helper waits for this process to exit before giving
# up and attempting the swap anyway - generous, but bounded so a stuck
# shutdown cannot hang the helper (and therefore the update) forever.
RELAUNCH_EXIT_TIMEOUT_S = 30

# Retry budget for the swap itself, run from the helper AFTER this process
# has exited - see build_relaunch_script. What is left to wait out at that
# point is not this process's own loaded DLLs (they are gone with it) but
# the same transient cause zapret/update.py's own sweep retries around: a
# cloud-lookup antivirus scan of a brand new, unsigned, never-seen-before
# file, which can run several seconds on a slow link. 10x1.5s (~15s worst
# case), same budget the old in-process retry this design replaces used.
RELAUNCH_RETRY_ATTEMPTS = 10
RELAUNCH_RETRY_DELAY_S = 1.5


@dataclass
class AppRelease:
    version: str  # "0.1.2" - leading "v" already stripped, see fetch_latest_release
    name: str
    notes: str  # the release body, Markdown - rendered as-is by the frontend
    html_url: str
    critical: bool
    # The asset a self-update would download - always the portable .zip now
    # (see _pick_update_asset), never a bare .exe: onedir means the exe
    # alone is not runnable without its _internal/ folder.
    asset_url: str | None = None  # None if the release has nothing usable
    asset_size: int = 0
    asset_digest: str | None = None  # "sha256:<hex>" from GitHub, or None if unavailable
    # True when asset_url points at a .zip, so bridgebox.exe + _internal/
    # have to be unpacked out of it first - see extract_release_from_archive.
    # Always True whenever asset_url is set, kept as its own field (rather
    # than inferred from asset_url's extension) so a caller can check intent
    # without re-parsing the URL.
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
    try:
        return tuple(int(part) for part in match.group(0).split("."))
    except ValueError:
        # SECURITY FIX: CPython caps int(str) at 4300 digits
        # (sys.get_int_max_str_digits) and raises past it. A GitHub
        # tag_name is free-form and length-unbounded, and
        # fetch_latest_release puts it into AppRelease.version with no
        # length check - so a hostile/broken release tag reached this
        # unguarded and took is_newer() down with it, breaking update
        # checks entirely for as long as that tag stayed "latest". Same
        # "unparseable -> not newer" contract as an empty match above.
        return ()


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


async def fetch_latest_release(
    session,
    *,
    url: str = RELEASES_URL,
    allowed_hosts: frozenset[str] = ALLOWED_HOSTS,
    require_https: bool = True,
) -> AppRelease:
    """`allowed_hosts`/`require_https` - see _require_allowed_host."""
    async with session.get(url, headers={"Accept": "application/vnd.github+json"}) as response:
        response.raise_for_status()
        payload = await response.json()

    tag = str(payload.get("tag_name") or "").strip()
    version = tag[1:] if tag[:1] in ("v", "V") else tag
    name = str(payload.get("name") or tag)
    notes = str(payload.get("body") or "")
    html_url = str(payload.get("html_url") or f"https://github.com/{REPO}/releases/latest")
    _require_allowed_host(html_url, allowed_hosts=allowed_hosts, require_https=require_https)

    chosen = _pick_update_asset(payload.get("assets") or [])
    asset_url: str | None = None
    asset_size = 0
    asset_digest: str | None = None
    asset_is_archive = False
    if chosen is not None:
        candidate = str(chosen.get("browser_download_url") or "")
        _require_allowed_host(candidate, allowed_hosts=allowed_hosts, require_https=require_https)
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


@dataclass
class ChangelogRelease:
    """One entry for the Info screen's "История версий" - a release's
    identity and body, nothing about its assets. A different, smaller shape
    than AppRelease on purpose: building one of those per release would
    re-run _pick_update_asset (and its digest/size bookkeeping) for every
    entry in the list just to throw the result away, for data this call
    never uses."""

    version: str  # leading "v" stripped, same convention as AppRelease
    name: str
    body: str
    date: str  # ISO date (published_at, truncated to the day)
    html_url: str


async def fetch_releases(session, *, url: str = ALL_RELEASES_URL) -> list[ChangelogRelease]:
    """Every published, non-draft release - newest first, GitHub's own order.

    Not filtered on `prerelease`: this app ships its normal betas as GitHub
    releases (see the existing "0.1.5 (b1)" changelog entry), so excluding
    them would hide most of the actual history."""
    async with session.get(url, headers={"Accept": "application/vnd.github+json"}) as response:
        response.raise_for_status()
        payload = await response.json()

    releases: list[ChangelogRelease] = []
    for entry in payload if isinstance(payload, list) else []:
        if entry.get("draft"):
            continue
        tag = str(entry.get("tag_name") or "").strip()
        version = tag[1:] if tag[:1] in ("v", "V") else tag
        if not version:
            continue
        html_url = str(entry.get("html_url") or f"https://github.com/{REPO}/releases/tag/{tag}")
        _require_allowed_host(html_url)
        published = str(entry.get("published_at") or entry.get("created_at") or "")
        releases.append(
            ChangelogRelease(
                version=version,
                name=str(entry.get("name") or tag),
                body=str(entry.get("body") or ""),
                date=published[:10],
                html_url=html_url,
            )
        )
    return releases


def _pick_update_asset(assets: list) -> dict | None:
    """The asset a self-update should download, or None if the release
    carries nothing usable.

    Always the portable .zip, never a bare .exe: onedir means the exe stub
    is not a runnable BridgeBox by itself any more - it needs the _internal/
    folder a release only ships inside the zip (see extract_release_from_
    archive), the same way it always needed zapret/ beside it. A bare .exe
    asset used to be preferred here (onefile did not have this problem) but
    would now update the launcher against a stale runtime - refused rather
    than silently accepted. Among several .zip assets the one whose name
    says "portable" wins, so a release that also carries some other archive
    does not send the updater after the wrong one."""
    zips = [
        asset for asset in assets
        if str(asset.get("name") or "").lower().endswith(".zip")
    ]
    if not zips:
        return None
    for asset in zips:
        if "portable" in str(asset.get("name") or "").lower():
            return asset
    return zips[0]


def _require_allowed_host(
    url: str, *, allowed_hosts: frozenset[str] = ALLOWED_HOSTS, require_https: bool = True
) -> None:
    """`allowed_hosts`/`require_https` exist for exactly one caller outside
    this module: test_app_update.py's synthetic end-to-end test, which
    downloads a real archive from a real (plain-http, localhost) server to
    exercise fetch_latest_release/download_exe's actual networking rather
    than a hand-mocked session - GitHub's own CDN is obviously not reachable
    from a test. Every production call site uses the defaults, which are
    exactly the check this always enforced."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if require_https and parsed.scheme != "https":
        raise ValueError(f"refusing a non-https release url: {url}")
    if parsed.hostname not in allowed_hosts:
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
    expected_size: int = 0,
    on_progress=None,
    attempts: int = DOWNLOAD_RETRY_ATTEMPTS,
    delay_s: float = DOWNLOAD_RETRY_DELAY_S,
    sleep=None,
    allowed_hosts: frozenset[str] = ALLOWED_HOSTS,
    require_https: bool = True,
) -> Path:
    """Stream the release's .exe asset to `dest`, aborting past max_bytes.

    Same retry-on-drop shape as zapret/update.py's download_archive: each
    attempt restarts from zero and truncates `dest` rather than resuming,
    because a Range request means trusting a server's byte offsets for a file
    that becomes the next thing this app runs as.

    `expected_size` - the release's own AppRelease.asset_size (from GitHub's
    Releases API, independent of whatever this specific request's response
    headers say). SECURITY FIX: a response with no Content-Length that ends
    early used to be accepted as a complete download - the IncompleteDownload
    guard below only ever fired when the header was present. Falling back to
    the size GitHub already told us the asset is closes that gap even when a
    CDN edge or proxy on the download itself omits the header.

    `allowed_hosts`/`require_https` - see _require_allowed_host."""
    import asyncio

    _require_allowed_host(url, allowed_hosts=allowed_hosts, require_https=require_https)
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    sleep = sleep if sleep is not None else asyncio.sleep

    for attempt in range(1, attempts + 1):
        received = 0
        try:
            async with session.get(url) as response:
                response.raise_for_status()
                _require_allowed_host(
                    str(response.url), allowed_hosts=allowed_hosts, require_https=require_https
                )
                total = int(response.headers.get("Content-Length") or 0) or expected_size
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
INTERNAL_DIR_NAME = "_internal"


def extract_release_from_archive(
    archive: Path,
    exe_dest: Path,
    internal_dest: Path,
    *,
    max_bytes: int = MAX_EXTRACTED_BYTES,
) -> Path:
    """Pull bridgebox.exe and its _internal/ folder out of a portable
    release .zip, writing the exe to `exe_dest` and every _internal/ member
    under `internal_dest`. Everything else the release ships (zapret/,
    certs/, config.yaml, ...) is ignored - self-update never touches those.

    bridgebox.exe is found by name at any depth, not by a fixed path: the
    release archive nests everything one level down
    (BridgeBox_Portable-v0.1.2b1/bridgebox.exe), and that folder name
    carries the version, so hard-coding it would break on the next release.
    Every other member extracted is one found under THAT SAME top-level
    folder, at the path it has relative to it - never the archive's own
    stored absolute-ish name, which is what keeps a hostile "../../evil"
    entry inert (unlike ZipFile.extractall, which honours them). That alone
    only rules out ".." landing BEFORE the top-level folder or the
    _internal/ prefix - an entry like "<release_root>/_internal/../../evil"
    still matches both, since both checks only look at the FIRST path
    segment. The resolved-path check right before each _internal/ write
    catches that: pathlib's joinpath() does not strip ".." the way
    os.path.normpath would, so nothing upstream of it actually stops such a
    member from landing outside internal_dest. The size cap is checked
    against the decompressed stream rather than any entry's own declared
    size, so a zip bomb cannot spend more disk than a real release would."""
    archive = Path(archive)
    exe_dest = Path(exe_dest)
    internal_dest = Path(internal_dest)
    written = 0

    def _write_member(info: zipfile.ZipInfo, target: Path) -> None:
        nonlocal written
        target.parent.mkdir(parents=True, exist_ok=True)
        with bundle.open(info) as source, target.open("wb") as handle:
            while chunk := source.read(64 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    handle.close()
                    exe_dest.unlink(missing_ok=True)
                    shutil.rmtree(internal_dest, ignore_errors=True)
                    raise ValueError(
                        f"{archive.name} exceeds {max_bytes} decompressed bytes - refusing"
                    )
                handle.write(chunk)

    with zipfile.ZipFile(archive) as bundle:
        exe_member = next(
            (
                info
                for info in bundle.infolist()
                if not info.is_dir()
                and PurePosixPath(info.filename).name.lower() == EXE_NAME_IN_ARCHIVE
            ),
            None,
        )
        if exe_member is None:
            raise RuntimeError(
                f"{archive.name} has no {EXE_NAME_IN_ARCHIVE} in it - "
                "not a BridgeBox portable archive"
            )
        release_root = PurePosixPath(exe_member.filename).parent
        _write_member(exe_member, exe_dest)

        found_internal = False
        for info in bundle.infolist():
            if info.is_dir() or info is exe_member:
                continue
            member_path = PurePosixPath(info.filename)
            try:
                relative = member_path.relative_to(release_root)
            except ValueError:
                continue  # not under the same top-level folder as the exe
            if not relative.parts or relative.parts[0] != INTERNAL_DIR_NAME:
                continue  # zapret/, certs/, config.yaml, ... - not ours to touch

            # SECURITY FIX (path traversal / zip-slip). relative_to() and the
            # INTERNAL_DIR_NAME check above both only look at the first path
            # segment - an entry named e.g.
            # "<release_root>/_internal/../../../../evil.exe" passes both,
            # and joinpath() below does not resolve ".." the way
            # os.path.normpath would, so without this the target could land
            # anywhere the (Administrator-elevated) update process can write.
            # Same is_relative_to()-on-a-resolved-path pattern config.py's
            # zapret.dir validator and zapret/process.py's _allowed_root
            # check already use for this exact class of problem.
            target = internal_dest.joinpath(*relative.parts[1:]).resolve()
            if not target.is_relative_to(internal_dest.resolve()):
                exe_dest.unlink(missing_ok=True)
                shutil.rmtree(internal_dest, ignore_errors=True)
                raise ValueError(
                    f"{archive.name} has a member that escapes the extraction "
                    f"root: {info.filename!r} - refusing"
                )
            _write_member(info, target)
            found_internal = True

    if not found_internal:
        raise RuntimeError(
            f"{archive.name} has no {INTERNAL_DIR_NAME}/ beside {EXE_NAME_IN_ARCHIVE} - "
            "not a BridgeBox onedir portable archive"
        )
    logger.info(
        "extracted %s + %s/ (%d bytes) from %s",
        EXE_NAME_IN_ARCHIVE, INTERNAL_DIR_NAME, written, archive.name,
    )
    return exe_dest


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
    """sys.executable while frozen IS the onedir .exe stub on disk - one of
    the two things self-update has to replace (see running_internal_dir()
    for the other). None in dev mode: there is no installed exe to swap, so
    callers fall back to sending the user to the release page."""
    if not getattr(sys, "frozen", False):
        return None
    return Path(sys.executable)


def running_internal_dir() -> Path | None:
    """Where _internal/ sits (or will sit) beside the running exe - the
    other half of a onedir install, see running_exe_path(). Returned even
    when the folder does not exist yet: the one real case that happens in is
    updating an OLD onefile install (no _internal/ at all) straight to the
    first onedir release, where there is nothing there yet for the relaunch
    script to back up - it just moves the staged folder into place. None
    only in dev mode, same as running_exe_path()."""
    exe = running_exe_path()
    return None if exe is None else exe.with_name(INTERNAL_DIR_NAME)


def _remove_path(path: Path) -> None:
    """A leftover backup from a prior update never cleaned up - could be
    either kind this module ever backs up, a file (bridgebox.exe.old) or a
    directory (_internal.old)."""
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


def _old(path: Path) -> Path:
    return path.with_name(path.name + _BACKUP_SUFFIX)


def build_relaunch_script(
    *,
    pid: int,
    exe_path: Path,
    exe_stage: Path,
    internal_path: Path,
    internal_stage: Path,
) -> str:
    """The .bat text a self-update's "restart now" writes to disk and spawns
    detached, right before this process exits - see
    desktop.Api.restart_after_app_update. See this module's own docstring
    for why the swap has to happen here and not in this process.

    What the script does, in order:
      1. Waits for `pid` (this process) to exit - PowerShell's Wait-Process,
         not a hand-rolled poll loop, bounded by RELAUNCH_EXIT_TIMEOUT_S so a
         stuck shutdown cannot hang the update forever.
      2. Grants Administrators/SYSTEM full control on both paths - the same
         ACL gap zapret/update.py's grant_full_control exists for, inlined
         here in icacls form since there is no Python interpreter running
         these commands to call that function from.
      3. Renames _internal/ (current -> .old, then stage -> current), then
         the exe the same way - each step individually retried
         (RELAUNCH_RETRY_ATTEMPTS/DELAY_S) in case an antivirus on-write scan
         of the freshly-staged files is still running. _internal/ goes
         first deliberately: it is the step that was actually failing in the
         wild, and a failure here leaves the old exe paired with the old
         _internal/ untouched - a safe, unchanged install. Every failure
         branch rolls back whatever already succeeded before giving up, so a
         partial failure never leaves a new exe paired with an old
         _internal/ (or vice versa) - the one pairing this module's own
         docstring says cannot run.
      4. Starts the new exe.
      5. Deletes itself - cmd.exe opens a running .bat with FILE_SHARE_DELETE
         (unlike LoadLibrary), so this is safe and leaves nothing behind."""
    exe_old = _old(exe_path)
    internal_old = _old(internal_path)
    grant = 'icacls "{path}" /grant *{admin}:(OI)(CI)F *{system}:(OI)(CI)F /T /C >nul 2>&1'
    return f"""@echo off
:: BridgeBox self-update helper - finishes swapping in the version this app
:: already downloaded and verified, then deletes itself. Safe to ignore or
:: delete if you find this sitting in your temp folder; it means an update
:: was interrupted before it could run.
::
:: SECURITY/UX FIX: cmd.exe decodes a .bat in the machine's OEM code page
:: (cp866 on a Russian Windows, cp1252 on a Western one), never UTF-8 - so
:: without this switch, every path below carrying a non-ASCII character (a
:: Cyrillic user-profile folder, the common case for this app's audience)
:: was handed to `move`/`rmdir` as mojibake, and the swap - including a
:: [critical] security update - silently failed. Paired with the UTF-8 BOM
:: this file is now written with (see restart_after_app_update), which is
:: what makes chcp's switch actually stick for a script read from disk
:: rather than typed at a live console.
chcp 65001 >nul
setlocal

set "RETRIES={RELAUNCH_RETRY_ATTEMPTS}"

powershell -NoProfile -Command "Wait-Process -Id {pid} -Timeout {RELAUNCH_EXIT_TIMEOUT_S} -ErrorAction SilentlyContinue" >nul 2>&1

{grant.format(path=str(internal_path), admin=SID_ADMINISTRATORS, system=SID_SYSTEM)}
{grant.format(path=str(exe_path), admin=SID_ADMINISTRATORS, system=SID_SYSTEM)}

if exist "{internal_old}" rmdir /s /q "{internal_old}" >nul 2>&1
if exist "{exe_old}" del /f /q "{exe_old}" >nul 2>&1

call :move_retry "{internal_path}" "{internal_old}"
if errorlevel 1 goto fail

call :move_retry "{internal_stage}" "{internal_path}"
if errorlevel 1 (
    call :move_retry "{internal_old}" "{internal_path}"
    goto fail
)

call :move_retry "{exe_path}" "{exe_old}"
if errorlevel 1 (
    call :move_retry "{internal_path}" "{internal_stage}"
    call :move_retry "{internal_old}" "{internal_path}"
    goto fail
)

call :move_retry "{exe_stage}" "{exe_path}"
if errorlevel 1 (
    call :move_retry "{exe_old}" "{exe_path}"
    call :move_retry "{internal_path}" "{internal_stage}"
    call :move_retry "{internal_old}" "{internal_path}"
    goto fail
)

start "" "{exe_path}"
del "%~f0"
exit /b 0

:move_retry
set "N=%RETRIES%"
:move_retry_loop
move /y %1 %2 >nul 2>&1
if not errorlevel 1 exit /b 0
set /a N-=1
if %N% gtr 0 (
    ping -n 2 127.0.0.1 >nul
    goto move_retry_loop
)
exit /b 1

:fail
exit /b 1
"""


def cleanup_stale_files(current_path: Path) -> bool:
    """Best-effort: delete a `.old` backup or `.new` stage file/folder a
    previous self-update left behind - the `.old` from a completed swap (see
    build_relaunch_script), the `.new` from one that downloaded but was
    never finished (interrupted, or the digest check refused it - see
    verify_exe_digest). Covers both halves of a onedir install:
    `current_path` (bridgebox.exe) and its _internal/ sibling.

    Called once at startup. By the time this (new) process is running,
    whatever briefly held `.old` locked (the relaunch script's own handle on
    it, or an antivirus scan) is done with it - but stays best-effort, since
    a leftover file/folder is harmless and the next launch will try again.

    Returns whether a `.old` backup was actually found - the signal main()
    uses to know this launch follows a self-update that just changed
    bridgebox.exe/_internal/ under it, so it can record a fresh integrity
    baseline instead of flagging its own update as tampering. A leftover
    `.new` stage does not count: that means a download never got as far as
    the relaunch script touching anything real."""
    candidates = [current_path, current_path.with_name(INTERNAL_DIR_NAME)]
    swapped = False
    for base in candidates:
        for suffix in (_BACKUP_SUFFIX, _STAGE_SUFFIX):
            stale = base.with_name(base.name + suffix)
            if not stale.exists():
                continue
            if suffix == _BACKUP_SUFFIX:
                swapped = True
            try:
                _remove_path(stale)
            except OSError as exc:
                logger.warning("could not remove leftover %s - will retry next launch", stale,
                                exc_info=exc)
    return swapped


def stage_path_for(current_path: Path) -> Path:
    return current_path.with_name(current_path.name + _STAGE_SUFFIX)
