"""The zapret updater. The extraction tests are the load-bearing ones: this
is the path by which a file that later runs as Administrator arrives from the
internet."""
import zipfile
from pathlib import Path

import pytest

from bridgebox.zapret import update


def _zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


PE = b"MZ\x90\x00" + b"\x00" * 60


# ---- version handling ----------------------------------------------------


def test_reads_the_real_ver_installed_format(tmp_path: Path):
    """The shipped file is CRLF-separated with no trailing newline."""
    (tmp_path / "ver.installed.txt").write_bytes(
        b'zapretver = "1.10.0"\r\nauthor = "Flowseal"'
    )

    assert update.read_installed_version(tmp_path) == "1.10.0"


def test_missing_or_garbage_version_reads_as_none(tmp_path: Path):
    assert update.read_installed_version(tmp_path) is None
    (tmp_path / "ver.installed.txt").write_text("nothing useful here", encoding="utf-8")
    assert update.read_installed_version(tmp_path) is None


def test_version_compare_is_numeric_not_lexicographic():
    """"1.10.0" > "1.9.9" is the case a string compare gets backwards, and it
    is the exact range this project sits in."""
    assert update.is_newer("1.10.0", "1.9.9") is True
    assert update.is_newer("1.10.0", "1.10.0") is False
    assert update.is_newer("1.9.9", "1.10.0") is False
    # Unparseable is never "newer": refusing to update on a version we do not
    # understand is the safe direction for a payload that runs elevated.
    assert update.is_newer("weird", "1.10.0") is False


# ---- extraction is the trust boundary ------------------------------------


def test_zip_slip_member_cannot_escape_the_destination(tmp_path: Path):
    archive = _zip(
        tmp_path / "r.zip",
        {"../../../evil.bin": b"payload", "winws.exe": PE},
    )
    dest = tmp_path / "staged"

    written = update.extract_allowed(archive, dest)

    # Only the basename is ever used, so the traversal lands flat inside dest.
    assert not (tmp_path.parent / "evil.bin").exists()
    assert (dest / "evil.bin").exists()
    assert set(written) == {"evil.bin", "winws.exe"}
    for path in written.values():
        assert dest in path.parents


def test_absolute_and_drive_letter_members_stay_inside(tmp_path: Path):
    archive = _zip(
        tmp_path / "r.zip",
        {"C:/Windows/System32/x.bin": b"x", "/etc/y.bin": b"y", "winws.exe": PE},
    )
    dest = tmp_path / "staged"

    written = update.extract_allowed(archive, dest)

    assert set(written) == {"x.bin", "y.bin", "winws.exe"}
    assert all(dest in p.parents for p in written.values())


def test_only_allowlisted_names_are_taken(tmp_path: Path):
    archive = _zip(
        tmp_path / "r.zip",
        {
            "winws.exe": PE,
            "WinDivert64.sys": b"driver",
            "quic_initial.bin": b"payload",
            "service.bat": b"@echo off",       # would break our launcher
            "README.md": b"docs",
            "strategies/general.bat": b"@echo off",
        },
    )

    written = update.extract_allowed(archive, tmp_path / "staged")

    assert set(written) == {"winws.exe", "WinDivert64.sys", "quic_initial.bin"}


def test_an_archive_without_winws_is_refused(tmp_path: Path):
    archive = _zip(tmp_path / "r.zip", {"stray.bin": b"x"})

    with pytest.raises(ValueError, match="no winws.exe"):
        update.extract_allowed(archive, tmp_path / "staged")


def test_a_winws_that_is_not_a_pe_image_is_refused(tmp_path: Path):
    """The one extracted file that gets executed as Administrator."""
    archive = _zip(tmp_path / "r.zip", {"winws.exe": b"#!/bin/sh\nrm -rf /"})

    with pytest.raises(ValueError, match="not a PE"):
        update.extract_allowed(archive, tmp_path / "staged")


def test_an_oversized_member_is_refused(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(update, "MAX_MEMBER_BYTES", 16)
    archive = _zip(tmp_path / "r.zip", {"winws.exe": PE, "big.bin": b"x" * 128})

    with pytest.raises(ValueError, match="refusing"):
        update.extract_allowed(archive, tmp_path / "staged")


def test_too_many_members_is_refused(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(update, "MAX_MEMBERS", 3)
    archive = _zip(tmp_path / "r.zip", {f"{i}.bin": b"x" for i in range(10)})

    with pytest.raises(ValueError, match="refusing"):
        update.extract_allowed(archive, tmp_path / "staged")


# ---- download host pinning ------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/x.zip",                     # not https
        "https://evil.example.com/x.zip",              # not github
        "https://github.com.evil.example/x.zip",       # suffix trick
    ],
)
async def test_download_refuses_an_unexpected_url(url, tmp_path: Path):
    with pytest.raises(ValueError):
        await update.download_archive(object(), url, tmp_path / "out.zip")


# ---- applying, with rollback ---------------------------------------------


def test_apply_replaces_files_and_removes_backups(tmp_path: Path):
    zapret = tmp_path / "zapret"
    zapret.mkdir()
    (zapret / "winws.exe").write_bytes(b"old")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "winws.exe").write_bytes(b"new")
    (staged / "fresh.bin").write_bytes(b"payload")

    applied = update.apply_update(
        {"winws.exe": staged / "winws.exe", "fresh.bin": staged / "fresh.bin"}, zapret
    )

    assert sorted(applied) == ["fresh.bin", "winws.exe"]
    assert (zapret / "winws.exe").read_bytes() == b"new"
    assert (zapret / "fresh.bin").read_bytes() == b"payload"
    assert list(zapret.glob("*.bak")) == []
    assert list(zapret.glob("*.tmp")) == []


def test_apply_rolls_back_every_file_when_one_write_fails(tmp_path: Path):
    """A half-applied payload is the state worth avoiding: winws.exe from one
    release with .bin payloads from another is a bypass that fails in ways
    nobody can diagnose."""
    zapret = tmp_path / "zapret"
    zapret.mkdir()
    (zapret / "a.bin").write_bytes(b"old-a")
    (zapret / "winws.exe").write_bytes(b"old-exe")

    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "a.bin").write_bytes(b"new-a")
    missing = staged / "winws.exe"  # deliberately never created

    with pytest.raises(update.ApplyFailed) as exc_info:
        update.apply_update({"a.bin": staged / "a.bin", "winws.exe": missing}, zapret)

    assert (zapret / "a.bin").read_bytes() == b"old-a"
    assert (zapret / "winws.exe").read_bytes() == b"old-exe"
    assert list(zapret.glob("*.bak")) == []
    # Everything went back, so retrying the update is safe - that is exactly
    # what an empty `unrestored` promises the caller.
    assert exc_info.value.unrestored == []


class _Locker:
    """Makes Path.replace fail with the Windows "file is locked" error a fixed
    number of times, then behave normally.

    Patched onto Path itself rather than onto one instance because apply_update
    builds its own Path objects internally."""

    def __init__(self, monkeypatch, *, target_name: str, failures: int, winerror: int = 5):
        self.remaining = failures
        self.target_name = target_name
        self.real = Path.replace
        locker = self

        def fake_replace(path_self, dest):
            if Path(dest).name == locker.target_name and locker.remaining > 0:
                locker.remaining -= 1
                raise PermissionError(13, "Отказано в доступе", str(dest), winerror)
            return locker.real(path_self, dest)

        monkeypatch.setattr(Path, "replace", fake_replace)


def test_apply_retries_a_locked_file_then_succeeds(tmp_path: Path, monkeypatch):
    """WinDivert64.sys is a kernel driver: winws.exe exiting does not release
    it, the service unloads it asynchronously a moment later. Waiting is the
    whole fix - the reported failure was a single attempt against that window."""
    zapret = tmp_path / "zapret"
    zapret.mkdir()
    (zapret / "WinDivert64.sys").write_bytes(b"old")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "WinDivert64.sys").write_bytes(b"new")

    _Locker(monkeypatch, target_name="WinDivert64.sys.bak", failures=2)
    slept: list[float] = []

    applied = update.apply_update(
        {"WinDivert64.sys": staged / "WinDivert64.sys"}, zapret, sleep=slept.append
    )

    assert applied == ["WinDivert64.sys"]
    assert (zapret / "WinDivert64.sys").read_bytes() == b"new"
    assert list(zapret.glob("*.bak")) == []
    assert list(zapret.glob("*.tmp")) == []
    assert slept == [update.LOCK_RETRY_DELAY_S, update.LOCK_RETRY_DELAY_S]


def test_apply_gives_up_after_the_retry_budget_and_rolls_back(tmp_path: Path, monkeypatch):
    zapret = tmp_path / "zapret"
    zapret.mkdir()
    (zapret / "WinDivert64.sys").write_bytes(b"old")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "WinDivert64.sys").write_bytes(b"new")

    _Locker(monkeypatch, target_name="WinDivert64.sys.bak", failures=999)
    slept: list[float] = []

    with pytest.raises(update.ApplyFailed) as exc_info:
        update.apply_update(
            {"WinDivert64.sys": staged / "WinDivert64.sys"}, zapret, sleep=slept.append
        )

    assert (zapret / "WinDivert64.sys").read_bytes() == b"old", "the original must survive"
    assert list(zapret.glob("*.bak")) == []
    assert exc_info.value.unrestored == []
    assert len(slept) == update.LOCK_RETRY_ATTEMPTS - 1, "one sleep between each pair of attempts"


def test_a_rollback_that_cannot_restore_keeps_the_backup_and_the_original_error(
    tmp_path: Path, monkeypatch
):
    """The rollback used to raise its OWN exception out of the except block,
    discarding the real cause - so the log named the wrong problem. And one
    locked file aborted the restore of every other file."""
    zapret = tmp_path / "zapret"
    zapret.mkdir()
    (zapret / "a.bin").write_bytes(b"old-a")
    staged = tmp_path / "staged"
    staged.mkdir()
    missing = staged / "a.bin"  # deliberately never created -> write fails

    # The restore (a.bin.bak -> a.bin) is what stays locked.
    _Locker(monkeypatch, target_name="a.bin", failures=999)
    slept: list[float] = []

    with pytest.raises(update.ApplyFailed) as exc_info:
        update.apply_update({"a.bin": missing}, zapret, sleep=slept.append)

    assert exc_info.value.unrestored == ["a.bin"]
    assert (zapret / "a.bin.bak").read_bytes() == b"old-a", "the only surviving copy must stay"
    assert isinstance(exc_info.value.cause, FileNotFoundError), "the real cause must survive"
    assert isinstance(exc_info.value.__cause__, FileNotFoundError)
    assert "a.bin" in str(exc_info.value)


def test_a_non_lock_error_is_not_retried(tmp_path: Path):
    """A missing source or a full disk will never succeed on retry. Burning the
    whole backoff budget to rediscover that makes a real error look like a hang."""
    zapret = tmp_path / "zapret"
    zapret.mkdir()
    staged = tmp_path / "staged"
    staged.mkdir()
    slept: list[float] = []

    with pytest.raises(update.ApplyFailed):
        update.apply_update({"winws.exe": staged / "never-created"}, zapret, sleep=slept.append)

    assert slept == []


def test_version_stamp_round_trips(tmp_path: Path):
    update.write_installed_version(tmp_path, "1.11.0")

    assert update.read_installed_version(tmp_path) == "1.11.0"
    assert 'author = "Flowseal"' in (tmp_path / "ver.installed.txt").read_text("utf-8")


# ---- only install what this build actually runs ---------------------------


def test_payloads_no_strategy_references_are_not_installed(tmp_path: Path):
    """The 1.10.0 release ships six payloads for profiles BridgeBox never
    ported (Discord, Steam, Tencent, the game port ranges). Copying them would
    leave files on disk that no .bat names - exactly what
    test_every_payload_file_is_used_by_some_strategy forbids, and that
    invariant is what finally caught the nine substituted fake-ClientHello
    payloads. Keep it true by construction instead of relaxing it."""
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    (strategies / "General.bat").write_text(
        '"%BIN%winws.exe" --dpi-desync-fake-quic="%BIN%quic_initial_www_google_com.bin"',
        encoding="utf-8",
    )

    staged = {
        "winws.exe": tmp_path / "winws.exe",
        "WinDivert64.sys": tmp_path / "WinDivert64.sys",
        "quic_initial_www_google_com.bin": tmp_path / "used.bin",
        "ACTIVE_DISCORD_UDP.bin": tmp_path / "discord.bin",
        "quic_initial_tencent_com.bin": tmp_path / "tencent.bin",
    }

    selected = update.select_for_install(staged, strategies)

    assert set(selected) == {
        "winws.exe",
        "WinDivert64.sys",
        "quic_initial_www_google_com.bin",
    }


def test_referenced_payloads_reads_every_strategy(tmp_path: Path):
    strategies = tmp_path / "strategies"
    strategies.mkdir()
    (strategies / "a.bat").write_text('--x="%BIN%one.bin"', encoding="utf-8")
    (strategies / "b.bat").write_text('--y="%BIN%two.bin" --z="%BIN%one.bin"', encoding="utf-8")

    assert update.referenced_payloads(strategies) == {"one.bin", "two.bin"}


def test_selection_against_the_real_strategies_keeps_every_shipped_payload():
    """Guards the live case: whatever the current 21 strategies name must
    survive selection, or an update would silently drop a payload the bypass
    depends on."""
    from bridgebox.paths import PROJECT_ROOT

    strategies_dir = PROJECT_ROOT / "zapret" / "strategies"
    on_disk = {p.name for p in (PROJECT_ROOT / "zapret").glob("*.bin")}

    referenced = update.referenced_payloads(strategies_dir)

    assert on_disk <= referenced, f"shipped payloads nothing references: {on_disk - referenced}"


# ---- Phase 2: adapting strategies a release adds -----------------------


def test_extract_original_strategies_takes_only_top_level_general_bats(tmp_path: Path):
    archive = _zip(
        tmp_path / "r.zip",
        {
            "general.bat": b"@echo off",
            "general (ALT13).bat": b"@echo off",
            "service.bat": b"@echo off",                      # not a strategy
            "strategies/general.bat": b"nested - not this one's basename",
            "discord.bat": b"@echo off",                      # doesn't start with general*
        },
    )

    written = update.extract_original_strategies(archive, tmp_path / "staged")

    assert set(written) == {"general.bat", "general (ALT13).bat"}
    assert written["general.bat"].read_bytes() == b"@echo off"


def test_extract_original_strategies_is_case_insensitive():
    """Flowseal ships General (EXP).bat with a capital G in some releases."""
    import zipfile as zf

    def make(tmp_path):
        archive = tmp_path / "r.zip"
        with zf.ZipFile(archive, "w") as z:
            z.writestr("General (EXP).bat", b"@echo off")
        return archive

    import tempfile
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        archive = make(tmp_path)
        written = update.extract_original_strategies(archive, tmp_path / "staged")
        assert set(written) == {"General (EXP).bat"}


# ---- strategy planning --------------------------------------------------

_ORIGINAL = (
    '"%BIN%winws.exe" --filter-tcp=80,443 --dpi-desync=fake --new '
    '--filter-udp=443 --dpi-desync=fake --dpi-desync-fake-quic="%BIN%q.bin"'
)
# Same shape, one flag different - stands in for a release that changed the
# parameters of a strategy already on disk. That case used to produce nothing.
_ORIGINAL_CHANGED = (
    '"%BIN%winws.exe" --filter-tcp=80,443 --dpi-desync=fakeddisorder --new '
    '--filter-udp=443 --dpi-desync=fake --dpi-desync-fake-quic="%BIN%q.bin"'
)


def _staged_original(tmp_path: Path, name: str, text: str = _ORIGINAL) -> dict[str, Path]:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return {name: path}


def _plan(tmp_path: Path, strategies_dir: Path, name="general.bat", text=_ORIGINAL, version="1.10.1"):
    return update.plan_strategies(
        _staged_original(tmp_path, name, text), strategies_dir, {"q.bin"}, version
    )


def test_plan_adapts_a_genuinely_new_qualifier(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()

    plan = _plan(tmp_path, strategies_dir, name="general (ALT13).bat")

    assert plan.skipped == []
    assert plan.added == ["General (ALT13).bat"]
    assert "--dpi-desync-fake-quic" in plan.write["General (ALT13).bat"]


def test_a_stamped_strategy_is_rewritten_in_place(tmp_path: Path):
    """The whole point of the rewrite: a release that CHANGES an existing
    strategy's parameters used to produce literally nothing, because the old
    stage_new_strategies skipped every target filename that already existed."""
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    first = _plan(tmp_path, strategies_dir, version="1.10.0")
    (strategies_dir / "General.bat").write_text(first.write["General.bat"], encoding="utf-8")

    plan = _plan(tmp_path, strategies_dir, text=_ORIGINAL_CHANGED, version="1.10.1")

    assert plan.updated == ["General.bat"]
    assert plan.forked == []
    assert "fakeddisorder" in plan.write["General.bat"]


def test_an_unstamped_shipped_strategy_is_forked_not_overwritten(tmp_path: Path):
    """Every one of the 21 strategies shipped today is unstamped, so this is
    the case that actually happens on the first real update."""
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    shipped = strategies_dir / "General.bat"
    shipped.write_text("@echo off\n:: hand-adapted, no stamp\n", encoding="utf-8")

    plan = _plan(tmp_path, strategies_dir)

    assert plan.forked == [("General.bat", "General (updated).bat")]
    assert plan.updated == []
    assert "General.bat" not in plan.write
    assert shipped.read_text(encoding="utf-8") == "@echo off\n:: hand-adapted, no stamp\n"


def test_a_hand_edited_stamped_strategy_is_forked_not_overwritten(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    first = _plan(tmp_path, strategies_dir, version="1.10.0")
    # Generated by us, then edited - the hash no longer matches its stamp.
    edited = first.write["General.bat"].replace("--dpi-desync=fake", "--dpi-desync=disorder")
    (strategies_dir / "General.bat").write_text(edited, encoding="utf-8")

    plan = _plan(tmp_path, strategies_dir, text=_ORIGINAL_CHANGED, version="1.10.1")

    assert plan.forked == [("General.bat", "General (updated).bat")]
    assert (strategies_dir / "General.bat").read_text(encoding="utf-8") == edited


def test_an_identical_adaptation_writes_nothing(tmp_path: Path):
    """A release that did not touch this strategy must cause no churn."""
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    first = _plan(tmp_path, strategies_dir, version="1.10.1")
    (strategies_dir / "General.bat").write_text(first.write["General.bat"], encoding="utf-8")

    plan = _plan(tmp_path, strategies_dir, version="1.10.1")

    assert plan.write == {}
    assert plan.added == plan.updated == plan.forked == []


def test_a_fork_target_that_is_also_user_modified_is_skipped_with_a_reason(tmp_path: Path):
    """Bounds the name growth: one extra file per strategy, never a chain."""
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    (strategies_dir / "General.bat").write_text("@echo off\n:: mine\n", encoding="utf-8")
    (strategies_dir / "General (updated).bat").write_text("@echo off\n:: also mine\n", encoding="utf-8")

    plan = _plan(tmp_path, strategies_dir)

    assert plan.write == {}
    assert len(plan.skipped) == 1
    assert plan.skipped[0][0] == "general.bat"


def test_plan_records_why_an_unusable_original_was_skipped(tmp_path: Path):
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()

    plan = update.plan_strategies(
        _staged_original(
            tmp_path,
            "general (weird).bat",
            '"%BIN%winws.exe" --filter-tcp=2053 --hostlist-domains=discord.media --dpi-desync=fake',
        ),
        strategies_dir,
        set(),
        "1.10.1",
    )

    assert plan.write == {}
    assert len(plan.skipped) == 1
    assert plan.skipped[0][0] == "general (weird).bat"


def test_generated_strategies_carry_the_blobcast_port_in_both_filters(tmp_path: Path):
    """test_strategy_assets pins this for files on disk; this pins it at the
    moment of generation, before anything reaches the disk. Both filters, or
    the Blobcast socket.io session gets no DPI bypass at all."""
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()

    plan = _plan(tmp_path, strategies_dir, name="general (ALT13).bat")

    for content in plan.write.values():
        assert "--wf-tcp=80,443,38203" in content
        assert "--filter-tcp=80,443,38203" in content


# ---- install_release: the whole post-download flow -----------------------


def _release_zip(tmp_path: Path, *, original: str = _ORIGINAL) -> Path:
    return _zip(
        tmp_path / "release.zip",
        {
            "winws.exe": PE,
            "WinDivert64.sys": b"driver",
            "q.bin": b"payload",
            "unused.bin": b"never referenced by any strategy",
            "general.bat": original.encode("utf-8"),
        },
    )


def _install_dirs(tmp_path: Path) -> tuple[Path, Path, Path]:
    zapret = tmp_path / "zapret"
    strategies = zapret / "strategies"
    strategies.mkdir(parents=True)
    return zapret, strategies, tmp_path / "stage"


def test_install_release_applies_binaries_before_strategies(tmp_path: Path, monkeypatch):
    """If the strategy write fails you are left with new binaries and old
    strategies, which runs. The reverse leaves a strategy naming a .bin that
    never arrived."""
    zapret, strategies, stage = _install_dirs(tmp_path)
    order: list[str] = []
    real_apply = update.apply_update

    def recording_apply(staged, target_dir, **kwargs):
        order.append("strategies" if Path(target_dir) == strategies else "binaries")
        return real_apply(staged, target_dir, **kwargs)

    monkeypatch.setattr(update, "apply_update", recording_apply)

    update.install_release(
        _release_zip(tmp_path),
        zapret_dir=zapret,
        strategies_dir=strategies,
        stage_dir=stage,
        version="1.10.1",
    )

    assert order == ["binaries", "strategies"]


def test_install_release_installs_a_payload_only_a_new_strategy_references(tmp_path: Path):
    """select_for_install is scoped to strategies already on disk. Without
    extra_refs, a brand-new strategy lands referencing a .bin that was filtered
    out - which is exactly the dangling state the payload invariant forbids."""
    zapret, strategies, stage = _install_dirs(tmp_path)

    applied, plan = update.install_release(
        _release_zip(tmp_path),
        zapret_dir=zapret,
        strategies_dir=strategies,
        stage_dir=stage,
        version="1.10.1",
    )

    assert plan.added == ["General.bat"]
    assert "q.bin" in applied, "the payload the new strategy names must be installed"
    assert "unused.bin" not in applied, "payloads nothing references must still be filtered out"
    assert (zapret / "q.bin").exists()


def test_install_release_writes_the_strategy_and_stamps_the_version(tmp_path: Path):
    zapret, strategies, stage = _install_dirs(tmp_path)

    update.install_release(
        _release_zip(tmp_path),
        zapret_dir=zapret,
        strategies_dir=strategies,
        stage_dir=stage,
        version="1.10.1",
    )

    written = (strategies / "General.bat").read_text(encoding="utf-8")
    assert "--dpi-desync-fake-quic" in written
    assert update.read_installed_version(zapret) == "1.10.1"
    # Left behind, a .bat.bak or .bat.tmp would not match discover_strategies'
    # glob - but it would still confuse anyone reading the folder.
    assert list(strategies.glob("*.bak")) == []
    assert list(strategies.glob("*.tmp")) == []


def test_install_release_never_removes_a_strategy(tmp_path: Path):
    """config.zapret.strategy holds a slug resolved against this directory.
    If an update could delete or rename a file, that setting would dangle and
    the next bridge start would fail on an unknown strategy."""
    zapret, strategies, stage = _install_dirs(tmp_path)
    (strategies / "General.bat").write_text("@echo off\n:: hand-adapted\n", encoding="utf-8")
    (strategies / "Alternative 7.bat").write_text("@echo off\n:: mine too\n", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in strategies.glob("*.bat")}

    _, plan = update.install_release(
        _release_zip(tmp_path),
        zapret_dir=zapret,
        strategies_dir=strategies,
        stage_dir=stage,
        version="1.10.1",
    )

    after = {p.name: p.read_bytes() for p in strategies.glob("*.bat")}
    for name, content in before.items():
        assert after[name] == content, f"{name} was modified or removed"
    assert plan.forked == [("General.bat", "General (updated).bat")]
    assert (strategies / "General (updated).bat").exists()


# ---- download resilience -------------------------------------------------


class _FlakyDownload:
    """A session whose GET drops the connection the first N times.

    ServerDisconnectedError is what the field actually reported: GitHub's CDN
    closes the connection part-way through, and one drop killed the whole
    update run."""

    def __init__(self, failures: int, body: bytes = b"payload", exc=None):
        import aiohttp

        self.failures = failures
        self.body = body
        self.calls = 0
        self.exc = exc or aiohttp.ServerDisconnectedError()

    def get(self, url):
        self.calls += 1
        fail = self.calls <= self.failures
        body, exc = self.body, self.exc

        class _Content:
            async def iter_chunked(self, _size):
                if fail:
                    raise exc
                yield body

        class _Response:
            status = 200
            url = "https://objects.githubusercontent.com/x.zip"
            headers = {"Content-Length": str(len(body))}
            content = _Content()

            def raise_for_status(self):
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        return _Response()


async def test_download_retries_a_dropped_connection(tmp_path: Path):
    session = _FlakyDownload(failures=2)
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    out = await update.download_archive(
        session,
        "https://objects.githubusercontent.com/x.zip",
        tmp_path / "r.zip",
        sleep=fake_sleep,
    )

    assert out.read_bytes() == b"payload"
    assert session.calls == 3
    assert slept == [update.DOWNLOAD_RETRY_DELAY_S, update.DOWNLOAD_RETRY_DELAY_S]


async def test_download_gives_up_after_the_budget_and_leaves_no_partial_file(tmp_path: Path):
    import aiohttp

    session = _FlakyDownload(failures=999)
    dest = tmp_path / "r.zip"

    async def fake_sleep(_seconds):
        return None

    with pytest.raises(aiohttp.ServerDisconnectedError):
        await update.download_archive(
            session, "https://objects.githubusercontent.com/x.zip", dest, sleep=fake_sleep
        )

    assert not dest.exists(), "a truncated archive must never be left for extract_allowed"


async def test_an_oversized_archive_is_refused_without_retrying(tmp_path: Path):
    """Our own size guard will fail identically every time - retrying it would
    just download the hostile payload three more times."""
    session = _FlakyDownload(failures=0, body=b"x" * 128)
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    with pytest.raises(ValueError, match="refusing"):
        await update.download_archive(
            session,
            "https://objects.githubusercontent.com/x.zip",
            tmp_path / "r.zip",
            max_bytes=16,
            sleep=fake_sleep,
        )

    assert session.calls == 1
    assert slept == []


# ---- what counts as "this strategy changed" ------------------------------


_TCP = '--filter-tcp=80,443 {tcp} --new '
_UDP = '--filter-udp=443 --dpi-desync=fake --dpi-desync-fake-quic="%BIN%q.bin"'


def _original(tcp_flags: str) -> str:
    return '"%BIN%winws.exe" ' + _TCP.format(tcp=tcp_flags) + _UDP


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param("--dpi-desync=fakeddisorder", id="desync-mode"),
        pytest.param("--dpi-desync=fake --dpi-desync-split-pos=3", id="added-split-pos"),
        pytest.param("--dpi-desync=fake --dpi-desync-ttl=4", id="added-ttl"),
        pytest.param("--dpi-desync=fake --dpi-desync-fooling=badseq", id="added-fooling"),
        pytest.param('--dpi-desync=fake --dpi-desync-fake-tls="%BIN%q.bin"', id="added-payload"),
        pytest.param("--dpi-desync=fake --dpi-desync-repeats=6", id="added-repeats"),
    ],
)
def test_any_changed_dpi_parameter_counts_as_an_update_not_just_payloads(
    tmp_path: Path, changed: str
):
    """The comparison is over the whole rendered invocation, not a payload
    list: every dpi-desync flag and value the release carries ends up in the
    file, so a changed mode, split position, TTL, fooling method or repeat
    count is as much an update as a swapped .bin."""
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()

    baseline = update.plan_strategies(
        _staged_original(tmp_path, "general.bat", _original("--dpi-desync=fake")),
        strategies_dir,
        {"q.bin"},
        "1.10.0",
    )
    (strategies_dir / "General.bat").write_text(baseline.write["General.bat"], encoding="utf-8")

    plan = update.plan_strategies(
        _staged_original(tmp_path, "general.bat", _original(changed)),
        strategies_dir,
        {"q.bin"},
        "1.10.1",
    )

    assert plan.updated == ["General.bat"], f"{changed} was not detected as a change"


def test_a_release_that_only_reorders_untouched_flags_is_not_an_update(tmp_path: Path):
    """The other half of the same claim: comparing rendered text must not
    report churn for a release that changed nothing this app uses."""
    strategies_dir = tmp_path / "strategies"
    strategies_dir.mkdir()
    text = _original("--dpi-desync=fake")

    first = update.plan_strategies(
        _staged_original(tmp_path, "general.bat", text), strategies_dir, {"q.bin"}, "1.10.1"
    )
    (strategies_dir / "General.bat").write_text(first.write["General.bat"], encoding="utf-8")

    plan = update.plan_strategies(
        _staged_original(tmp_path, "general.bat", text), strategies_dir, {"q.bin"}, "1.10.1"
    )

    assert plan.write == {} and plan.updated == []


# ---- the WinError 5 escalation ladder -------------------------------------


def test_a_backup_that_will_not_delete_does_not_fail_a_finished_update(
    tmp_path: Path, monkeypatch
):
    """The new file is already in place by the time the backups are cleaned
    up. This used to be a bare unlink outside the try, so one still-held .bak
    raised out of a COMPLETED update and the user was told it had failed."""
    zapret = tmp_path / "zapret"
    zapret.mkdir()
    (zapret / "winws.exe").write_bytes(b"old")
    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "winws.exe").write_bytes(b"new")

    real_unlink = Path.unlink

    def stubborn(self, *args, **kwargs):
        if self.suffix == ".bak":
            raise PermissionError(13, "Отказано в доступе", str(self), 5)
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", stubborn)
    scheduled: list[Path] = []
    monkeypatch.setattr(update, "delete_on_reboot", lambda path: scheduled.append(path) or True)

    applied = update.apply_update({"winws.exe": staged / "winws.exe"}, zapret)

    assert applied == ["winws.exe"]
    assert (zapret / "winws.exe").read_bytes() == b"new"
    assert [p.name for p in scheduled] == ["winws.exe.bak"]


def test_permissions_are_granted_by_sid_not_by_group_name():
    """icacls resolves account names in the machine's own language, so
    "Administrators" fails outright on a Russian Windows - a trap this repo has
    already paid for once in tls/ca.py."""
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)

        class Result:
            returncode = 0

        return Result()

    update.grant_full_control(Path("C:/zapret"), runner=runner)

    assert calls, "icacls was never invoked"
    joined = " ".join(calls[0])
    assert update.SID_ADMINISTRATORS in joined
    assert update.SID_SYSTEM in joined
    assert "Administrators:" not in joined


def test_a_refused_icacls_does_not_raise():
    """A wrong ACL is one possible cause of WinError 5, not a certainty.
    Refusing to even try the update because icacls said no would trade a
    maybe-fixable failure for a guaranteed one."""

    def exploding(cmd, **kwargs):
        raise OSError("icacls missing")

    assert update.grant_full_control(Path("C:/zapret"), runner=exploding) is False


def test_install_release_fixes_permissions_before_touching_anything(
    tmp_path: Path, monkeypatch
):
    """Order matters: after the first rename has already failed is too late."""
    order: list[str] = []
    monkeypatch.setattr(
        update, "grant_full_control", lambda directory, **kw: order.append("icacls") or True
    )
    real_extract = update.extract_allowed
    monkeypatch.setattr(
        update,
        "extract_allowed",
        lambda archive, dest: order.append("extract") or real_extract(archive, dest),
    )

    zapret = tmp_path / "zapret"
    (zapret / "strategies").mkdir(parents=True)
    archive = _zip(tmp_path / "release.zip", {"winws.exe": PE})

    update.install_release(
        archive,
        zapret_dir=zapret,
        strategies_dir=zapret / "strategies",
        stage_dir=tmp_path / "stage",
        version="1.10.1",
    )

    assert order[0] == "icacls"
