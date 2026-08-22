"""Discovery for the non-Steam ("Прочие копии") launch-options feature:
drive enumeration, the recursive shortcut/jet-file walk, shortcut and jet
editing, and backup/apply/revert orchestration."""
import asyncio
from pathlib import Path

import pytest
import pythoncom
import win32com.client

from bridgebox import other_launch


class FakeWin32Drives:
    """Stands in for win32api/win32file - real drive enumeration is not
    something a test should touch. drive_types maps a drive letter string
    (e.g. "C:\\\\") to a Win32 GetDriveType() integer."""

    def __init__(self, drive_types: dict[str, int]):
        self._drive_types = drive_types

    def GetLogicalDriveStrings(self):
        return "\x00".join(self._drive_types) + "\x00\x00"

    def GetDriveType(self, drive):
        return self._drive_types[drive]


def test_list_fixed_drives_excludes_cdrom_and_remote(tmp_path: Path):
    c_drive = str(tmp_path)
    fake = FakeWin32Drives({
        c_drive: other_launch._DRIVE_FIXED,
        "D:\\": other_launch._DRIVE_CDROM,
        "E:\\": other_launch._DRIVE_REMOTE,
        "F:\\": other_launch._DRIVE_REMOVABLE,
    })

    result = other_launch.list_fixed_drives(win32api_module=fake, win32file_module=fake)

    assert result == [Path(c_drive), Path("F:\\")]


def test_list_fixed_drives_excludes_unmounted_and_unknown_drives():
    fake = FakeWin32Drives({
        "G:\\": other_launch._DRIVE_NO_ROOT_DIR,
        "H:\\": other_launch._DRIVE_UNKNOWN,
    })

    assert other_launch.list_fixed_drives(win32api_module=fake, win32file_module=fake) == []


def _make_shortcut(path: Path, target: Path, arguments: str = "") -> None:
    """Creates a REAL .lnk via the real Shell COM object - per the design
    doc's testing section, .lnk has no safe fake-format seam the way VDF's
    brace-delimited text has, so this exercises the actual Windows Shell
    API rather than a hand-rolled stand-in for it.

    Explicitly CoInitialize/CoUninitialize-wrapped like other_launch's own
    COM calls (see other_launch._com_initialized) rather than relying on
    pywin32's implicit auto-init on first use: that implicit fallback only
    covers a thread's very first COM touch, and this test module's own
    calls into other_launch already do real CoInitialize/CoUninitialize
    round-trips on the same (main) thread - a bare Dispatch() call after
    that would raise "CoInitialize has not been called".

    The actual COM work happens in a nested function so `shell`/`shortcut`
    are released when THAT frame returns - i.e. before CoUninitialize runs
    in the finally below. Holding them as locals in this outer frame
    instead would keep them alive until _make_shortcut itself returns,
    releasing a COM pointer after the apartment's already torn down, which
    crashes the interpreter with an access violation (same hazard
    other_launch.py's own COM functions guard against)."""
    def _create() -> None:
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortCut(str(path))
        shortcut.TargetPath = str(target)
        shortcut.Arguments = arguments
        shortcut.Save()

    pythoncom.CoInitialize()
    try:
        _create()
    finally:
        pythoncom.CoUninitialize()


def test_scan_finds_a_jackbox_shortcut_by_its_targets_folder_name(tmp_path: Path):
    game_dir = tmp_path / "Games" / "The Jackbox Party Pack 9"
    game_dir.mkdir(parents=True)
    target = game_dir / "JackboxPartyPack9.exe"
    target.write_text("")
    shortcut_path = tmp_path / "Desktop" / "Play TJPP9.lnk"
    shortcut_path.parent.mkdir()
    _make_shortcut(shortcut_path, target)

    result = other_launch.scan_for_other_copies([tmp_path])

    assert result == [
        other_launch.OtherCandidate(kind="shortcut", path=str(shortcut_path), name="The Jackbox Party Pack 9"),
    ]


def test_scan_ignores_a_shortcut_to_something_that_is_not_jackbox(tmp_path: Path):
    game_dir = tmp_path / "Games" / "Half-Life 2"
    game_dir.mkdir(parents=True)
    target = game_dir / "hl2.exe"
    target.write_text("")
    shortcut_path = tmp_path / "Play HL2.lnk"
    _make_shortcut(shortcut_path, target)

    assert other_launch.scan_for_other_copies([tmp_path]) == []


def test_scan_matches_a_known_single_by_its_targets_folder_name(tmp_path: Path):
    game_dir = tmp_path / "Drawful 2"
    game_dir.mkdir()
    target = game_dir / "Drawful2.exe"
    target.write_text("")
    shortcut_path = tmp_path / "Play Drawful.lnk"
    _make_shortcut(shortcut_path, target)

    result = other_launch.scan_for_other_copies([tmp_path])

    assert result == [
        other_launch.OtherCandidate(kind="shortcut", path=str(shortcut_path), name="Drawful 2"),
    ]


def test_scan_finds_a_jbg_config_jet_file(tmp_path: Path):
    game_dir = tmp_path / "The Jackbox Party Pack 7"
    game_dir.mkdir()
    jet_path = game_dir / other_launch.JET_FILE_NAME
    jet_path.write_text('{"serverUrl": "127.0.0.1:8443"}', encoding="utf-8")

    result = other_launch.scan_for_other_copies([tmp_path])

    assert result == [
        other_launch.OtherCandidate(kind="jet", path=str(jet_path), name="The Jackbox Party Pack 7"),
    ]


def test_scan_skips_os_reserved_folders(tmp_path: Path):
    reserved = tmp_path / "System Volume Information"
    reserved.mkdir()
    (reserved / other_launch.JET_FILE_NAME).write_text('{"serverUrl": ""}', encoding="utf-8")

    assert other_launch.scan_for_other_copies([tmp_path]) == []


def test_scan_reports_progress_by_folder_count(tmp_path: Path):
    for i in range(3):
        (tmp_path / f"folder{i}").mkdir()
    seen: list[int] = []

    other_launch.scan_for_other_copies([tmp_path], progress_cb=seen.append)

    assert seen  # at least the final flush call
    assert seen[-1] >= 4  # tmp_path itself plus its 3 children


def test_read_shortcut_arguments_returns_the_current_value(tmp_path: Path):
    target = tmp_path / "game.exe"
    target.write_text("")
    shortcut_path = tmp_path / "Play.lnk"
    _make_shortcut(shortcut_path, target, arguments="-windowed")

    assert other_launch.read_shortcut_arguments(str(shortcut_path)) == "-windowed"


def test_read_shortcut_arguments_is_empty_string_not_none_when_unset(tmp_path: Path):
    target = tmp_path / "game.exe"
    target.write_text("")
    shortcut_path = tmp_path / "Play.lnk"
    _make_shortcut(shortcut_path, target)

    assert other_launch.read_shortcut_arguments(str(shortcut_path)) == ""


def test_write_shortcut_arguments_round_trips(tmp_path: Path):
    target = tmp_path / "game.exe"
    target.write_text("")
    shortcut_path = tmp_path / "Play.lnk"
    _make_shortcut(shortcut_path, target, arguments="-windowed")

    other_launch.write_shortcut_arguments(str(shortcut_path), "-windowed -jbg.config serverUrl=127.0.0.1:8443")

    assert other_launch.read_shortcut_arguments(str(shortcut_path)) == "-windowed -jbg.config serverUrl=127.0.0.1:8443"


def test_read_shortcut_arguments_raises_shortcut_error_for_a_missing_file(tmp_path: Path):
    with pytest.raises(other_launch.ShortcutError):
        other_launch.read_shortcut_arguments(str(tmp_path / "missing.lnk"))


def test_read_jet_server_url_returns_the_current_value(tmp_path: Path):
    jet_path = tmp_path / other_launch.JET_FILE_NAME
    jet_path.write_text('{"serverUrl": "127.0.0.1:8443", "otherField": true}', encoding="utf-8")

    assert other_launch.read_jet_server_url(jet_path) == "127.0.0.1:8443"


def test_read_jet_server_url_is_none_when_the_key_is_missing(tmp_path: Path):
    jet_path = tmp_path / other_launch.JET_FILE_NAME
    jet_path.write_text('{"otherField": true}', encoding="utf-8")

    assert other_launch.read_jet_server_url(jet_path) is None


def test_write_jet_server_url_sets_the_field_and_keeps_everything_else(tmp_path: Path):
    jet_path = tmp_path / other_launch.JET_FILE_NAME
    jet_path.write_text('{"otherField": true}', encoding="utf-8")

    other_launch.write_jet_server_url(jet_path, "127.0.0.1:8443")

    import json
    data = json.loads(jet_path.read_text(encoding="utf-8"))
    assert data == {"otherField": True, "serverUrl": "127.0.0.1:8443"}


def test_write_jet_server_url_removes_the_key_when_value_is_none(tmp_path: Path):
    """Reverting a file that never had serverUrl before this feature touched
    it must leave the file exactly as it started - not with an explicit
    null the original never had."""
    jet_path = tmp_path / other_launch.JET_FILE_NAME
    jet_path.write_text('{"serverUrl": "127.0.0.1:8443", "otherField": true}', encoding="utf-8")

    other_launch.write_jet_server_url(jet_path, None)

    import json
    data = json.loads(jet_path.read_text(encoding="utf-8"))
    assert data == {"otherField": True}


def test_load_backups_returns_empty_dict_when_no_file_exists(tmp_path: Path):
    assert other_launch.load_backups(tmp_path) == {}


def test_save_and_load_backups_round_trip(tmp_path: Path):
    data = {str(tmp_path / "Play.lnk"): {"kind": "shortcut", "value": "-windowed"}}

    assert other_launch.save_backups(tmp_path, data) is True
    assert other_launch.load_backups(tmp_path) == data


def test_backups_use_their_own_file_not_steams(tmp_path: Path):
    from bridgebox import steam_launch
    steam_launch.save_backups(tmp_path, {"852600": {"hadLaunchOptions": True, "value": "-dx11"}})

    assert other_launch.load_backups(tmp_path) == {}
    assert (tmp_path / other_launch.BACKUP_FILE_NAME).name != steam_launch.BACKUP_FILE_NAME


def test_apply_launch_options_patches_a_shortcut_and_backs_up_the_old_value(tmp_path: Path):
    target = tmp_path / "game.exe"
    target.write_text("")
    shortcut_path = tmp_path / "Play.lnk"
    _make_shortcut(shortcut_path, target, arguments="-windowed")

    result = other_launch.apply_launch_options(
        tmp_path, [{"kind": "shortcut", "path": str(shortcut_path)}], 8443,
    )

    assert result["ok"] is True
    assert result["results"][str(shortcut_path)] == {"ok": True, "error": None}
    assert other_launch.read_shortcut_arguments(str(shortcut_path)) == "-windowed -jbg.config serverUrl=127.0.0.1:8443"
    backups = other_launch.load_backups(tmp_path)
    assert backups[str(shortcut_path)] == {"kind": "shortcut", "value": "-windowed"}


def test_apply_launch_options_patches_a_jet_file_and_backs_up_the_old_value(tmp_path: Path):
    jet_path = tmp_path / other_launch.JET_FILE_NAME
    jet_path.write_text('{"otherField": true}', encoding="utf-8")

    result = other_launch.apply_launch_options(
        tmp_path, [{"kind": "jet", "path": str(jet_path)}], 8443,
    )

    assert result["ok"] is True
    assert other_launch.read_jet_server_url(jet_path) == "127.0.0.1:8443"
    backups = other_launch.load_backups(tmp_path)
    assert backups[str(jet_path)] == {"kind": "jet", "value": None}


def test_apply_launch_options_never_overwrites_an_existing_backup(tmp_path: Path):
    """Same rule as the Steam feature: re-applying after an earlier apply
    must not clobber the ORIGINAL value with the already-patched one."""
    jet_path = tmp_path / other_launch.JET_FILE_NAME
    jet_path.write_text('{"serverUrl": "203.0.113.5:9000"}', encoding="utf-8")
    other_launch.apply_launch_options(tmp_path, [{"kind": "jet", "path": str(jet_path)}], 8443)

    other_launch.apply_launch_options(tmp_path, [{"kind": "jet", "path": str(jet_path)}], 9999)

    backups = other_launch.load_backups(tmp_path)
    assert backups[str(jet_path)] == {"kind": "jet", "value": "203.0.113.5:9000"}


def test_apply_launch_options_reports_invalid_json_per_item(tmp_path: Path):
    jet_path = tmp_path / other_launch.JET_FILE_NAME
    jet_path.write_text("not json", encoding="utf-8")

    result = other_launch.apply_launch_options(tmp_path, [{"kind": "jet", "path": str(jet_path)}], 8443)

    assert result["ok"] is False
    assert result["results"][str(jet_path)]["error"] == "invalid_json"


def test_apply_launch_options_reports_a_missing_shortcut_per_item(tmp_path: Path):
    missing = tmp_path / "missing.lnk"

    result = other_launch.apply_launch_options(tmp_path, [{"kind": "shortcut", "path": str(missing)}], 8443)

    assert result["ok"] is False
    assert result["results"][str(missing)]["error"] == "lnk_unreadable"


def test_revert_launch_options_restores_a_shortcuts_arguments(tmp_path: Path):
    target = tmp_path / "game.exe"
    target.write_text("")
    shortcut_path = tmp_path / "Play.lnk"
    _make_shortcut(shortcut_path, target, arguments="-windowed")
    other_launch.apply_launch_options(tmp_path, [{"kind": "shortcut", "path": str(shortcut_path)}], 8443)

    result = other_launch.revert_launch_options(tmp_path, [{"kind": "shortcut", "path": str(shortcut_path)}])

    assert result["ok"] is True
    assert other_launch.read_shortcut_arguments(str(shortcut_path)) == "-windowed"
    assert str(shortcut_path) not in other_launch.load_backups(tmp_path)


def test_revert_launch_options_removes_a_jet_files_serverurl_if_it_never_had_one(tmp_path: Path):
    jet_path = tmp_path / other_launch.JET_FILE_NAME
    jet_path.write_text('{"otherField": true}', encoding="utf-8")
    other_launch.apply_launch_options(tmp_path, [{"kind": "jet", "path": str(jet_path)}], 8443)

    result = other_launch.revert_launch_options(tmp_path, [{"kind": "jet", "path": str(jet_path)}])

    assert result["ok"] is True
    assert other_launch.read_jet_server_url(jet_path) is None


def test_revert_launch_options_reports_no_backup(tmp_path: Path):
    result = other_launch.revert_launch_options(tmp_path, [{"kind": "jet", "path": str(tmp_path / "x.jet")}])

    assert result["ok"] is False
    assert result["results"][str(tmp_path / "x.jet")]["error"] == "no_backup"


def test_scan_finds_a_shortcut_from_a_real_background_thread(tmp_path: Path):
    """Regression for the COM-not-initialized bug: desktop.py's Api methods
    call into this module via asyncio.to_thread, landing on a worker thread
    that never calls CoInitialize. Without _com_initialized() wrapping
    scan_for_other_copies, every Dispatch("WScript.Shell") call there raises
    pywintypes.com_error("CoInitialize has not been called"), which
    _resolve_shortcut_target swallows - so the shortcut silently fails to
    resolve and the scan comes back empty instead of finding it."""
    game_dir = tmp_path / "Games" / "The Jackbox Party Pack 9"
    game_dir.mkdir(parents=True)
    target = game_dir / "JackboxPartyPack9.exe"
    target.write_text("")
    shortcut_path = tmp_path / "Desktop" / "Play TJPP9.lnk"
    shortcut_path.parent.mkdir()
    _make_shortcut(shortcut_path, target)

    async def _scan_on_worker_thread():
        return await asyncio.to_thread(other_launch.scan_for_other_copies, [tmp_path])

    result = asyncio.run(_scan_on_worker_thread())

    assert result == [
        other_launch.OtherCandidate(kind="shortcut", path=str(shortcut_path), name="The Jackbox Party Pack 9"),
    ]


def test_read_shortcut_arguments_works_from_a_real_background_thread(tmp_path: Path):
    """Same regression as above but for the shortcut-editing side (apply's
    read_shortcut_arguments/write_shortcut_arguments call path)."""
    target = tmp_path / "game.exe"
    target.write_text("")
    shortcut_path = tmp_path / "Play.lnk"
    _make_shortcut(shortcut_path, target, arguments="-windowed")

    async def _read_on_worker_thread():
        return await asyncio.to_thread(other_launch.read_shortcut_arguments, str(shortcut_path))

    assert asyncio.run(_read_on_worker_thread()) == "-windowed"


def test_apply_launch_options_reports_invalid_json_for_a_non_dict_jet_root(tmp_path: Path):
    """A jet file whose JSON root is a list, not an object - read_jet_server_url's
    .get("serverUrl") has nothing to call on a list, so this must not escape
    as a raw AttributeError and abort the whole batch."""
    jet_path = tmp_path / other_launch.JET_FILE_NAME
    jet_path.write_text("[]", encoding="utf-8")

    result = other_launch.apply_launch_options(tmp_path, [{"kind": "jet", "path": str(jet_path)}], 8443)

    assert result["ok"] is False
    assert result["results"][str(jet_path)] == {"ok": False, "error": "invalid_json"}


def test_applying_twice_to_the_same_shortcut_replaces_not_appends_the_flag(tmp_path: Path):
    target = tmp_path / "game.exe"
    target.write_text("")
    shortcut_path = tmp_path / "Play.lnk"
    _make_shortcut(shortcut_path, target, arguments="-windowed")

    other_launch.apply_launch_options(tmp_path, [{"kind": "shortcut", "path": str(shortcut_path)}], 8443)
    other_launch.apply_launch_options(tmp_path, [{"kind": "shortcut", "path": str(shortcut_path)}], 9999)

    final_args = other_launch.read_shortcut_arguments(str(shortcut_path))
    assert final_args.count("-jbg.config serverUrl=") == 1
    assert "-jbg.config serverUrl=127.0.0.1:9999" in final_args
    assert "127.0.0.1:8443" not in final_args
