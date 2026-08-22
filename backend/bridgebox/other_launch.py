"""Find Jackbox shortcuts and jbg.config.jet files across every local drive,
and write/restore the serverUrl connection setting BridgeBox's README
otherwise asks the user to edit by hand.

Non-Steam ("Прочие копии") only - see
docs/superpowers/specs/2026-08-22-other-copies-launch-options-design.md.
Steam copies go through steam_launch.py/steam_vdf.py instead - a Steam
install's launch options live inside one shared localconfig.vdf, while this
feature edits many independent per-install files (a shortcut's own
Arguments field, or a game's own jbg.config.jet), which is why the two
features don't share an apply/revert implementation despite doing
conceptually the same thing.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import pythoncom
import win32api as _real_win32api
import win32com.client as _real_win32com_client
import win32file as _real_win32file

from . import steam_launch

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _com_initialized():
    """COM must be initialized on whatever thread calls into WScript.Shell.
    desktop.py's Api methods hop onto asyncio.to_thread worker threads to
    reach this module, and those threads never call CoInitialize on their
    own - every Dispatch() there would otherwise raise a pywintypes.com_error
    ("CoInitialize has not been called"). CoInitialize is refcounted per
    thread, so calling it on a thread that already has COM initialized
    (pytest's main thread, or nesting this context manager) is a harmless
    no-op that the matching CoUninitialize balances."""
    pythoncom.CoInitialize()
    try:
        yield
    finally:
        pythoncom.CoUninitialize()


JET_FILE_NAME = "jbg.config.jet"

# Win32 GetDriveType() return values (winbase.h) - stable OS constants, not
# something pywin32 exposes as named attributes on every install.
_DRIVE_UNKNOWN = 0
_DRIVE_NO_ROOT_DIR = 1
_DRIVE_REMOVABLE = 2
_DRIVE_FIXED = 3
_DRIVE_REMOTE = 4
_DRIVE_CDROM = 5
_DRIVE_RAMDISK = 6

_EXCLUDED_DRIVE_TYPES = frozenset({_DRIVE_UNKNOWN, _DRIVE_NO_ROOT_DIR, _DRIVE_CDROM, _DRIVE_REMOTE})


def list_fixed_drives(
    *, win32api_module=_real_win32api, win32file_module=_real_win32file,
) -> list[Path]:
    """Every drive letter Windows reports, minus optical/network drives and
    the two "not really a drive" types - GetLogicalDriveStrings lists every
    mounted drive letter, GetDriveType says what kind each one is. Removable
    drives (USB) are kept: the design doc's exclusion list names only CDROM
    and REMOTE, not REMOVABLE."""
    raw = win32api_module.GetLogicalDriveStrings()
    letters = [d for d in raw.split("\x00") if d]
    return [
        Path(letter) for letter in letters
        if win32file_module.GetDriveType(letter) not in _EXCLUDED_DRIVE_TYPES
    ]


# OS-reserved folders that can never hold a game install - skipped at any
# depth, not just at a drive root, since a duplicate-named folder can exist
# anywhere on a drive.
_EXCLUDED_FOLDER_NAMES = frozenset({"windows", "$recycle.bin", "system volume information"})

# How many folders between progress callbacks - frequent enough that a long
# scan's readout doesn't look frozen, coarse enough that the callback itself
# (an Api attribute write, see desktop.py) isn't invoked in a hot loop.
_PROGRESS_EVERY = 25


@dataclass(frozen=True)
class OtherCandidate:
    kind: str  # "shortcut" or "jet"
    path: str
    name: str


def _resolve_shortcut_target(
    path: Path, *, win32com_client=_real_win32com_client, shell=None, on_error=None,
) -> str | None:
    """The shortcut's TargetPath, or None if the .lnk can't be read at all
    (locked, malformed) or has no target set - both cases mean "we can't
    tell if this is Jackbox", so scanning silently skips it rather than
    surfacing a checklist item with no name to show.

    shell, if given, is a WScript.Shell dispatch object reused across many
    calls (scan_for_other_copies dispatches one for the whole walk instead of
    once per .lnk); on_error, if given, is called with the caught exception
    so a caller can tell "genuinely no target" apart from "COM blew up" -
    both look the same to the return value on purpose, since a single
    unreadable shortcut is still just skipped."""
    try:
        shell = shell or win32com_client.Dispatch("WScript.Shell")
        target = shell.CreateShortCut(str(path)).TargetPath
    except Exception as exc:
        logger.debug("could not read shortcut %s: %s", path, exc)
        if on_error is not None:
            on_error(exc)
        return None
    return target or None


def _shortcut_target_matches_jackbox(target: str) -> bool:
    target_path = Path(target)
    return (
        steam_launch.is_jackbox_title(target_path.stem)
        or steam_launch.is_jackbox_title(target_path.parent.name)
    )


def scan_for_other_copies(
    roots: list[Path], *, win32com_client=_real_win32com_client, progress_cb=None,
) -> list[OtherCandidate]:
    """Walks every root looking for two independent things: .lnk shortcuts
    whose resolved target matches a Jackbox install, and files literally
    named jbg.config.jet. progress_cb, if given, is called with the number
    of folders checked so far every _PROGRESS_EVERY folders, plus once more
    at the very end - a full-drive walk can take minutes, and the caller
    (desktop.Api) uses this to answer other_scan_progress() polls."""
    candidates: list[OtherCandidate] = []
    folders_checked = 0
    unreadable_count = 0

    def _count_unreadable(exc: Exception) -> None:
        nonlocal unreadable_count
        unreadable_count += 1

    def _walk(shell) -> None:
        nonlocal folders_checked
        for root in roots:
            for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=lambda exc: None):
                dirnames[:] = [d for d in dirnames if d.lower() not in _EXCLUDED_FOLDER_NAMES]
                folders_checked += 1
                if progress_cb is not None and folders_checked % _PROGRESS_EVERY == 0:
                    progress_cb(folders_checked)
                for filename in filenames:
                    lower = filename.lower()
                    if lower.endswith(".lnk"):
                        lnk_path = Path(dirpath) / filename
                        target = _resolve_shortcut_target(
                            lnk_path, win32com_client=win32com_client, shell=shell, on_error=_count_unreadable,
                        )
                        if target and _shortcut_target_matches_jackbox(target):
                            candidates.append(
                                OtherCandidate(kind="shortcut", path=str(lnk_path), name=Path(target).parent.name)
                            )
                    elif lower == JET_FILE_NAME:
                        jet_path = Path(dirpath) / filename
                        candidates.append(OtherCandidate(kind="jet", path=str(jet_path), name=jet_path.parent.name))

    with _com_initialized():
        # The dispatched shell is passed as an argument into _walk() rather
        # than held in a local here, so it (and the per-shortcut objects
        # created inside _walk) are released when _walk's own frame is torn
        # down - which happens before this with-block exits and calls
        # CoUninitialize. Holding `shell` directly in this frame instead
        # would keep it alive until scan_for_other_copies itself returns,
        # i.e. after COM has already been uninitialized on this thread -
        # releasing it then reliably crashes the interpreter (access
        # violation), the same hazard read_shortcut_arguments/
        # write_shortcut_arguments avoid the same way.
        _walk(win32com_client.Dispatch("WScript.Shell"))
    if unreadable_count:
        logger.warning("skipped %d shortcut(s) that could not be read", unreadable_count)
    if progress_cb is not None:
        progress_cb(folders_checked)
    return candidates


class ShortcutError(Exception):
    """A .lnk could not be read or written via COM - locked, malformed, or
    gone. Callers translate this to a per-item error code rather than
    letting a raw pywintypes.com_error escape this module."""


def read_shortcut_arguments(path: str, *, win32com_client=_real_win32com_client) -> str:
    # WScript.Shell's CreateShortCut never raises for a missing path - per
    # its own docs it silently hands back a fresh, unsaved, all-empty
    # shortcut object instead of opening the existing one. Without this
    # check, a shortcut that vanished between scan and apply would read as
    # "no Arguments" (empty string) rather than "missing" - and worse, a
    # write through that same object would CREATE a new .lnk at that path,
    # which breaks the "patch only, never create" rule.
    if not Path(path).is_file():
        raise ShortcutError(f"{path} does not exist")

    def _read() -> str:
        shell = win32com_client.Dispatch("WScript.Shell")
        return shell.CreateShortCut(path).Arguments or ""

    try:
        # _read()'s own stack frame - and with it every COM wrapper object
        # it created - is torn down the instant it returns, which happens
        # *inside* the with-block, before CoUninitialize runs. Doing the COM
        # work directly in this function's body instead would leave `shell`
        # bound in THIS frame until this function itself returns, i.e. after
        # the with-block's CoUninitialize already ran - releasing a COM
        # pointer on an apartment that's already been torn down, which is a
        # reliable way to crash the interpreter with an access violation.
        with _com_initialized():
            return _read()
    except Exception as exc:
        raise ShortcutError(str(exc)) from exc


def write_shortcut_arguments(path: str, arguments: str, *, win32com_client=_real_win32com_client) -> None:
    if not Path(path).is_file():
        raise ShortcutError(f"{path} does not exist")

    def _write() -> None:
        shell = win32com_client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(path)
        shortcut.Arguments = arguments
        shortcut.Save()

    try:
        # See read_shortcut_arguments - same reason this delegates to a
        # nested function rather than holding `shell`/`shortcut` locally.
        with _com_initialized():
            _write()
    except Exception as exc:
        raise ShortcutError(str(exc)) from exc


def read_jet_server_url(path: Path) -> str | None:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("serverUrl")


def write_jet_server_url(path: Path, value: str | None) -> None:
    """value is the new serverUrl on apply, or None on revert when the
    original file had no serverUrl key at all - pops the key rather than
    writing a null, so a reverted file matches its pre-apply state exactly.
    Real JSON, unlike Steam's shared localconfig.vdf, so a full
    load/mutate/dump round-trip is safe - this file belongs to one game
    install and has nothing else whose formatting matters to preserve."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if value is None:
        data.pop("serverUrl", None)
    else:
        data["serverUrl"] = value
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


BACKUP_FILE_NAME = "other_launch_backup.json"


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


def apply_launch_options(project_root: Path, items: list[dict], port: int) -> dict:
    """Patches every selected shortcut/jet file in place, backing up each
    item's prior value first (skipping any path that already has a real
    backup - never overwrite a backup with an already-patched value, same
    rule as the Steam feature). Unlike Steam, nothing needs to be closed
    first: each file belongs to one game install, not a shared cross-app
    store, so there is no "the running app might rewrite this underneath us"
    hazard to wait out."""
    flag = f"-jbg.config serverUrl=127.0.0.1:{port}"
    server_url = f"127.0.0.1:{port}"
    backups = load_backups(project_root)
    results: dict[str, dict] = {}
    for item in items:
        path, kind = item["path"], item["kind"]
        try:
            if kind == "shortcut":
                current = read_shortcut_arguments(path)
                if path not in backups:
                    backups[path] = {"kind": "shortcut", "value": current}
                write_shortcut_arguments(path, steam_launch.merge_launch_option(current, flag))
            elif kind == "jet":
                current = read_jet_server_url(Path(path))
                if path not in backups:
                    backups[path] = {"kind": "jet", "value": current}
                write_jet_server_url(Path(path), server_url)
            else:
                results[path] = {"ok": False, "error": "unknown_kind"}
                continue
            results[path] = {"ok": True, "error": None}
        except ShortcutError:
            results[path] = {"ok": False, "error": "lnk_unreadable"}
        except (json.JSONDecodeError, AttributeError, TypeError):
            # AttributeError/TypeError: a jet file whose JSON root parsed
            # fine but isn't a dict (e.g. "[]") - .get() has nothing to call
            # on a list, same "content isn't shaped how we expect" case as a
            # decode failure, so it gets the same per-item error code rather
            # than escaping and aborting the whole batch.
            results[path] = {"ok": False, "error": "invalid_json"}
        except FileNotFoundError:
            results[path] = {"ok": False, "error": "not_found"}
        except OSError:
            results[path] = {"ok": False, "error": "file_error"}
    save_backups(project_root, backups)
    return {"ok": all(r["ok"] for r in results.values()), "error": None, "results": results}


def revert_launch_options(project_root: Path, items: list[dict]) -> dict:
    backups = load_backups(project_root)
    results: dict[str, dict] = {}
    for item in items:
        path, kind = item["path"], item["kind"]
        backup = backups.get(path)
        if backup is None:
            results[path] = {"ok": False, "error": "no_backup"}
            continue
        try:
            if kind == "shortcut":
                write_shortcut_arguments(path, backup["value"])
            elif kind == "jet":
                write_jet_server_url(Path(path), backup["value"])
            else:
                results[path] = {"ok": False, "error": "unknown_kind"}
                continue
            results[path] = {"ok": True, "error": None}
            del backups[path]
        except ShortcutError:
            results[path] = {"ok": False, "error": "lnk_unreadable"}
        except (AttributeError, TypeError):
            results[path] = {"ok": False, "error": "invalid_json"}
        except OSError:
            results[path] = {"ok": False, "error": "file_error"}
    save_backups(project_root, backups)
    return {"ok": all(r["ok"] for r in results.values()), "error": None, "results": results}
