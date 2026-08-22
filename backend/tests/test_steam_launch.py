"""Finding Steam itself and the library folders its games can live in."""
from pathlib import Path

import pytest

from bridgebox import steam_launch, steam_vdf


class FakeWinreg:
    """Stands in for the winreg module - real registry access is not
    something a test should touch."""

    HKEY_CURRENT_USER = object()

    def __init__(self, steam_path: str | None):
        self._steam_path = steam_path

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def OpenKey(self, hive, subkey):
        if self._steam_path is None:
            raise OSError("registry key not found")
        return self._Key()

    def QueryValueEx(self, key, name):
        return self._steam_path, 1


def test_find_steam_path_reads_the_registry_key(tmp_path: Path):
    steam_dir = tmp_path / "Steam"
    steam_dir.mkdir()

    result = steam_launch.find_steam_path(winreg_module=FakeWinreg(str(steam_dir)))

    assert result == steam_dir


def test_find_steam_path_is_none_when_the_registry_key_is_missing():
    assert steam_launch.find_steam_path(winreg_module=FakeWinreg(None)) is None


def test_find_steam_path_is_none_when_the_registry_points_at_a_missing_folder(tmp_path: Path):
    missing = tmp_path / "does-not-exist"

    result = steam_launch.find_steam_path(winreg_module=FakeWinreg(str(missing)))

    assert result is None


def test_find_steam_path_is_none_when_the_registry_value_is_empty():
    result = steam_launch.find_steam_path(winreg_module=FakeWinreg(""))

    assert result is None


def test_list_library_folders_always_includes_the_main_steam_path(tmp_path: Path):
    steam_path = tmp_path / "Steam"
    (steam_path / "steamapps").mkdir(parents=True)

    assert steam_launch.list_library_folders(steam_path) == [steam_path]


def test_list_library_folders_finds_additional_drives(tmp_path: Path):
    steam_path = tmp_path / "Steam"
    (steam_path / "steamapps").mkdir(parents=True)
    other_library = tmp_path / "D_SteamLibrary"
    other_library.mkdir()
    (steam_path / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n'
        '\t"0"\n\t{\n\t\t"path"\t\t"' + str(other_library).replace("\\", "\\\\") + '"\n\t}\n'
        "}\n",
        encoding="utf-8",
    )

    result = steam_launch.list_library_folders(steam_path)

    assert steam_path in result
    assert other_library in result


def test_list_library_folders_ignores_a_missing_libraryfolders_vdf(tmp_path: Path):
    steam_path = tmp_path / "Steam"
    steam_path.mkdir()

    assert steam_launch.list_library_folders(steam_path) == [steam_path]


@pytest.mark.parametrize("name", [
    "The Jackbox Party Pack 9",
    "The Jackbox Party Pack",
    "the jackbox survey scramble",
    "THE JACKBOX NAUGHTY PACK",
    "Drawful 2",
    "Quiplash",
    "Quiplash 2 InterLASHional",
    "Fibbage XL",
])
def test_is_jackbox_title_matches_known_titles(name):
    assert steam_launch.is_jackbox_title(name) is True


@pytest.mark.parametrize("name", ["Half-Life 2", "Drawful", "Fibbage", ""])
def test_is_jackbox_title_rejects_unrelated_or_similar_titles(name):
    assert steam_launch.is_jackbox_title(name) is False


def _write_manifest(steamapps: Path, appid: str, name: str) -> None:
    (steamapps / f"appmanifest_{appid}.acf").write_text(
        '"AppState"\n{\n'
        f'\t"appid"\t\t"{appid}"\n'
        f'\t"name"\t\t"{name}"\n'
        "}\n",
        encoding="utf-8",
    )


def test_scan_installed_jackbox_games_finds_matches_and_skips_others(tmp_path: Path):
    steamapps = tmp_path / "steamapps"
    steamapps.mkdir()
    _write_manifest(steamapps, "852600", "The Jackbox Party Pack 7")
    _write_manifest(steamapps, "400", "Half-Life 2")
    _write_manifest(steamapps, "1234", "Quiplash")

    games = steam_launch.scan_installed_jackbox_games(tmp_path)

    assert {g.appid for g in games} == {"852600", "1234"}
    assert {g.name for g in games} == {"The Jackbox Party Pack 7", "Quiplash"}


def test_scan_installed_jackbox_games_searches_every_library(tmp_path: Path):
    main = tmp_path / "Steam"
    (main / "steamapps").mkdir(parents=True)
    other = tmp_path / "OtherLibrary"
    (other / "steamapps").mkdir(parents=True)
    _write_manifest(other / "steamapps", "999", "The Jackbox Party Pack 10")
    (main / "steamapps" / "libraryfolders.vdf").write_text(
        '"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"'
        + str(other).replace("\\", "\\\\") + '"\n\t}\n}\n',
        encoding="utf-8",
    )

    games = steam_launch.scan_installed_jackbox_games(main)

    assert [g.appid for g in games] == ["999"]


# Task 4 tests
def _steam_id64(id3: int) -> int:
    return id3 + 76561197960265728


def _write_loginusers(steam_path: Path, entries: dict[int, bool]) -> None:
    """entries: {steam_id3: is_most_recent}. Expressed as a `Timestamp`
    (larger = more recently logged in) since that's the field a real Steam
    install actually writes - an earlier version of this fixture used
    `MostRecent`, which turned out not to exist at all in a real
    loginusers.vdf (checked directly, not assumed)."""
    body = ""
    for i, (id3, most_recent) in enumerate(entries.items()):
        timestamp = 2_000_000_000 if most_recent else 1_000_000_000 + i
        body += (
            f'\t"{_steam_id64(id3)}"\n\t{{\n'
            f'\t\t"Timestamp"\t\t"{timestamp}"\n\t}}\n'
        )
    (steam_path / "config").mkdir(parents=True, exist_ok=True)
    (steam_path / "config" / "loginusers.vdf").write_text(
        f'"users"\n{{\n{body}}}\n', encoding="utf-8"
    )


def test_find_active_local_config_resolves_the_most_recently_logged_in_user(tmp_path: Path):
    steam_path = tmp_path / "Steam"
    _write_loginusers(steam_path, {111: False, 222: True})
    config_dir = steam_path / "userdata" / "222" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "localconfig.vdf").write_text("", encoding="utf-8")

    result = steam_launch.find_active_local_config(steam_path)

    assert result == config_dir / "localconfig.vdf"


def test_find_active_local_config_is_none_when_no_entry_has_a_timestamp(tmp_path: Path):
    """The real ambiguous case now that MostRecent doesn't exist: a
    loginusers.vdf whose entries carry no parseable Timestamp at all."""
    steam_path = tmp_path / "Steam"
    (steam_path / "config").mkdir(parents=True)
    (steam_path / "config" / "loginusers.vdf").write_text(
        f'"users"\n{{\n\t"{_steam_id64(111)}"\n\t{{\n\t\t"AccountName"\t\t"whatever"\n\t}}\n}}\n',
        encoding="utf-8",
    )

    assert steam_launch.find_active_local_config(steam_path) is None


def test_find_active_local_config_is_none_when_loginusers_is_missing(tmp_path: Path):
    steam_path = tmp_path / "Steam"
    steam_path.mkdir()

    assert steam_launch.find_active_local_config(steam_path) is None


def test_filter_configurable_games_keeps_only_titles_with_an_existing_block(tmp_path: Path):
    config_path = tmp_path / "localconfig.vdf"
    config_path.write_text(
        '"apps"\n{\n\t"852600"\n\t{\n\t\t"LaunchOptions"\t\t""\n\t}\n}\n',
        encoding="utf-8",
    )
    games = [
        steam_launch.SteamGame(appid="852600", name="The Jackbox Party Pack 7"),
        steam_launch.SteamGame(appid="1234", name="Quiplash"),
    ]

    result = steam_launch.filter_configurable_games(config_path, games)

    assert [g.appid for g in result] == ["852600"]


# Task 5 tests
class FakeProcessRunner:
    """Simulates tasklist/-shutdown/taskkill calls. `running` is a mutable
    flag the test controls, so quit_steam's poll loop can be driven
    deterministically without a real sleep."""

    def __init__(self, *, running: bool = True, shutdown_works: bool = True):
        self.running = running
        self.shutdown_works = shutdown_works
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(cmd)
        if "tasklist" in cmd[0]:
            stdout = "steam.exe  1234 Console  1  50,000 K" if self.running else "INFO: No tasks"
            return _Result(0, stdout)
        if "-shutdown" in cmd:
            if self.shutdown_works:
                self.running = False
            return _Result(0, "")
        if "taskkill" in cmd[0]:
            self.running = False
            return _Result(0, "")
        return _Result(0, "")


class _Result:
    def __init__(self, returncode, stdout):
        self.returncode = returncode
        self.stdout = stdout


def test_is_steam_running_true_when_tasklist_finds_it():
    runner = FakeProcessRunner(running=True)
    assert steam_launch.is_steam_running(runner=runner) is True


def test_is_steam_running_false_when_tasklist_does_not_find_it():
    runner = FakeProcessRunner(running=False)
    assert steam_launch.is_steam_running(runner=runner) is False


def test_quit_steam_succeeds_via_graceful_shutdown(tmp_path: Path):
    runner = FakeProcessRunner(running=True, shutdown_works=True)

    result = steam_launch.quit_steam(tmp_path, runner=runner, sleep=lambda s: None)

    assert result is True
    assert any("-shutdown" in call for call in runner.calls)
    assert not any("taskkill" in call[0] for call in runner.calls)


def test_quit_steam_escalates_to_forced_kill_when_graceful_fails(tmp_path: Path):
    runner = FakeProcessRunner(running=True, shutdown_works=False)

    result = steam_launch.quit_steam(tmp_path, runner=runner, sleep=lambda s: None)

    assert result is True
    assert any("taskkill" in call[0] for call in runner.calls)


def test_quit_steam_is_a_noop_when_steam_is_not_running(tmp_path: Path):
    runner = FakeProcessRunner(running=False)

    result = steam_launch.quit_steam(tmp_path, runner=runner, sleep=lambda s: None)

    assert result is True
    assert any("tasklist" in call[0] for call in runner.calls)
    assert not any("taskkill" in call[0] or "-shutdown" in call for call in runner.calls)


def test_launch_steam_starts_the_exe(tmp_path: Path):
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append(cmd)

    result = steam_launch.launch_steam(tmp_path, popen=fake_popen)

    assert result is True
    assert calls[0][0] == str(tmp_path / "steam.exe")


def test_launch_steam_returns_false_on_failure(tmp_path: Path):
    def failing_popen(cmd, **kwargs):
        raise OSError("no such file")

    assert steam_launch.launch_steam(tmp_path, popen=failing_popen) is False


# Task 6 tests
def _make_fake_steam_tree(tmp_path: Path, *, launch_options: str = "") -> Path:
    steam_path = tmp_path / "Steam"
    _write_loginusers(steam_path, {222: True})
    config_dir = steam_path / "userdata" / "222" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "localconfig.vdf").write_text(
        '"apps"\n{\n\t"852600"\n\t{\n'
        f'\t\t"LaunchOptions"\t\t"{launch_options}"\n\t}}\n}}\n',
        encoding="utf-8",
    )
    return steam_path


def test_load_backups_returns_empty_dict_when_no_file_exists(tmp_path: Path):
    assert steam_launch.load_backups(tmp_path) == {}


def test_save_and_load_backups_round_trip(tmp_path: Path):
    data = {"852600": {"hadLaunchOptions": True, "value": "-dx11"}}

    assert steam_launch.save_backups(tmp_path, data) is True
    assert steam_launch.load_backups(tmp_path) == data


def test_apply_launch_options_end_to_end(tmp_path: Path):
    steam_path = _make_fake_steam_tree(tmp_path, launch_options="-dx11")
    runner = FakeProcessRunner(running=True, shutdown_works=True)
    popen_calls = []

    result = steam_launch.apply_launch_options(
        steam_path, tmp_path, ["852600"], 8443,
        runner=runner, popen=lambda cmd, **kw: popen_calls.append(cmd), sleep=lambda s: None,
    )

    assert result["ok"] is True
    assert result["results"]["852600"]["ok"] is True
    assert result["steamRelaunched"] is True
    assert popen_calls, "Steam should have been relaunched"

    config_path = steam_path / "userdata" / "222" / "config" / "localconfig.vdf"
    new_text = config_path.read_text(encoding="utf-8")
    assert "-jbg.config serverUrl=127.0.0.1:8443" in new_text
    assert "-dx11" in new_text  # pre-existing option preserved, not overwritten

    backups = steam_launch.load_backups(tmp_path)
    assert backups["852600"]["value"] == "-dx11"


def test_apply_launch_options_preserves_bytes_outside_the_target_block(tmp_path: Path):
    """Regression: the old read_text(errors="replace")/write_text round trip
    silently destroyed any non-UTF-8 byte anywhere in the file (permanently
    replaced with U+FFFD on write-back) and rewrote every LF to os.linesep.
    Both must now be impossible: the file is read/written as bytes with
    surrogateescape, so a stray non-UTF-8 byte (as if from a legacy Latin-1
    persona name) and LF endings OUTSIDE the touched appid's block survive
    byte-for-byte."""
    steam_path = tmp_path / "Steam"
    _write_loginusers(steam_path, {222: True})
    config_dir = steam_path / "userdata" / "222" / "config"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "localconfig.vdf"
    raw = (
        b'"apps"\n{\n'
        b'\t"852600"\n\t{\n\t\t"LaunchOptions"\t\t"-dx11"\n\t}\n'
        b'\t"999"\n\t{\n\t\t"PersonaName"\t\t"Caf\xe9"\n\t}\n'
        b"}\n"
    )
    config_path.write_bytes(raw)
    runner = FakeProcessRunner(running=False)

    result = steam_launch.apply_launch_options(
        steam_path, tmp_path, ["852600"], 8443,
        runner=runner, popen=lambda cmd, **kw: None, sleep=lambda s: None,
    )

    assert result["ok"] is True
    new_bytes = config_path.read_bytes()
    assert b'"PersonaName"\t\t"Caf\xe9"' in new_bytes  # non-UTF-8 byte survives byte-for-byte
    assert b"\r\n" not in new_bytes  # LF outside the edit never got rewritten to CRLF


def test_apply_launch_options_replaces_a_pre_existing_jbg_flag_instead_of_duplicating(tmp_path: Path):
    steam_path = _make_fake_steam_tree(tmp_path, launch_options="-jbg.config serverUrl=127.0.0.1:9999")
    runner = FakeProcessRunner(running=False)

    steam_launch.apply_launch_options(
        steam_path, tmp_path, ["852600"], 8443,
        runner=runner, popen=lambda cmd, **kw: None, sleep=lambda s: None,
    )

    config_path = steam_path / "userdata" / "222" / "config" / "localconfig.vdf"
    text = config_path.read_text(encoding="utf-8")
    assert text.count("-jbg.config") == 1
    assert "127.0.0.1:8443" in text


def test_apply_launch_options_aborts_without_editing_when_steam_will_not_close(tmp_path: Path):
    steam_path = _make_fake_steam_tree(tmp_path)
    config_path = steam_path / "userdata" / "222" / "config" / "localconfig.vdf"
    original = config_path.read_text(encoding="utf-8")

    class NeverDies(FakeProcessRunner):
        def __call__(self, cmd, **kwargs):
            self.calls.append(cmd)
            if "tasklist" in cmd[0]:
                return _Result(0, "steam.exe  1234 Console  1  50,000 K")
            return _Result(1, "")

    result = steam_launch.apply_launch_options(
        steam_path, tmp_path, ["852600"], 8443,
        runner=NeverDies(), popen=lambda cmd, **kw: None, sleep=lambda s: None,
    )

    assert result["ok"] is False
    assert result["error"] == "could_not_close_steam"
    assert config_path.read_text(encoding="utf-8") == original


def test_revert_launch_options_restores_the_backed_up_value(tmp_path: Path):
    steam_path = _make_fake_steam_tree(tmp_path, launch_options="-dx11")
    runner = FakeProcessRunner(running=False)
    steam_launch.apply_launch_options(
        steam_path, tmp_path, ["852600"], 8443,
        runner=runner, popen=lambda cmd, **kw: None, sleep=lambda s: None,
    )

    result = steam_launch.revert_launch_options(
        steam_path, tmp_path, ["852600"],
        runner=runner, popen=lambda cmd, **kw: None, sleep=lambda s: None,
    )

    assert result["ok"] is True
    config_path = steam_path / "userdata" / "222" / "config" / "localconfig.vdf"
    assert steam_vdf.read_launch_options(config_path.read_text(encoding="utf-8"), "852600") == "-dx11"
    assert "852600" not in steam_launch.load_backups(tmp_path)


def test_revert_launch_options_reports_missing_backup(tmp_path: Path):
    steam_path = _make_fake_steam_tree(tmp_path)
    runner = FakeProcessRunner(running=False)

    result = steam_launch.revert_launch_options(
        steam_path, tmp_path, ["852600"],
        runner=runner, popen=lambda cmd, **kw: None, sleep=lambda s: None,
    )

    assert result["results"]["852600"]["error"] == "no_backup"


def test_apply_launch_options_does_not_overwrite_backup_on_re_apply(tmp_path: Path):
    """Regression test for Finding 1: re-applying to the same appid should
    preserve the original pristine backup, not overwrite it with the
    already-patched value."""
    steam_path = _make_fake_steam_tree(tmp_path, launch_options="-dx11")
    runner = FakeProcessRunner(running=False)

    # First apply: patches the flag in and backs up the original value
    result1 = steam_launch.apply_launch_options(
        steam_path, tmp_path, ["852600"], 8443,
        runner=runner, popen=lambda cmd, **kw: None, sleep=lambda s: None,
    )
    assert result1["ok"] is True
    config_path = steam_path / "userdata" / "222" / "config" / "localconfig.vdf"
    text_after_first = config_path.read_text(encoding="utf-8")
    assert "-jbg.config serverUrl=127.0.0.1:8443" in text_after_first

    # Verify backup has the original value
    backups_after_first = steam_launch.load_backups(tmp_path)
    assert backups_after_first["852600"]["value"] == "-dx11"

    # Second apply: re-applies to the same appid (e.g. user changed their mind,
    # re-selected the same game before reverting)
    result2 = steam_launch.apply_launch_options(
        steam_path, tmp_path, ["852600"], 8443,
        runner=runner, popen=lambda cmd, **kw: None, sleep=lambda s: None,
    )
    assert result2["ok"] is True

    # The critical assertion: backup must still hold the original "-dx11",
    # not the already-patched value from after the first apply
    backups_after_second = steam_launch.load_backups(tmp_path)
    assert backups_after_second["852600"]["value"] == "-dx11"
