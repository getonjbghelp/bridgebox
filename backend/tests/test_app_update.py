from pathlib import Path

import pytest

from bridgebox import app_update


# ---- is_newer / version parsing ------------------------------------------


@pytest.mark.parametrize(
    "latest,installed,expected",
    [
        ("0.2.0", "0.1.9", True),
        ("0.1.10", "0.1.9", True),  # numeric, not lexicographic
        ("0.1.9", "0.1.9", False),
        ("0.1.8", "0.1.9", False),
        ("v0.2.0", "0.1.9", True),  # leading "v" stripped
        ("0.2.0", "0.1.2b1", True),  # pre-release suffix on the installed side
        ("garbage", "0.1.9", False),  # unparseable latest -> never "newer"
        ("0.2.0", "", False),
        ("", "0.1.9", False),
    ],
)
def test_is_newer(latest, installed, expected):
    assert app_update.is_newer(latest, installed) is expected


def test_is_newer_never_raises_on_junk():
    assert app_update.is_newer("¯\\_(ツ)_/¯", "0.1.0") is False
    assert app_update.is_newer("0.1.0", "¯\\_(ツ)_/¯") is False


# ---- critical detection ---------------------------------------------------


def test_is_critical_matches_in_the_title():
    assert app_update.is_critical("[CRITICAL] Security fix", "") is True


def test_is_critical_matches_in_the_body_case_insensitively():
    assert app_update.is_critical("v0.1.3", "Please update - [critical] token leak fix") is True


def test_is_critical_is_false_for_an_ordinary_release():
    assert app_update.is_critical("v0.1.3", "- Fixed a UI glitch\n- Minor cleanup") is False


# ---- fetch_latest_release --------------------------------------------------


class _FakeSession:
    def __init__(self, payload: dict):
        self._payload = payload
        self.requested_url = None

    def get(self, url, *, headers=None):
        self.requested_url = url
        payload = self._payload

        class _Response:
            def raise_for_status(self):
                return None

            async def json(self):
                return payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        return _Response()


def _release_payload(*, assets=None, tag="v0.1.4"):
    return {
        "tag_name": tag,
        "name": tag.lstrip("v"),
        "body": "",
        "html_url": f"https://github.com/getonjbghelp/bridgebox/releases/tag/{tag}",
        "assets": assets or [],
    }


async def test_fetch_latest_release_parses_tag_name_and_strips_the_v():
    session = _FakeSession(
        {
            "tag_name": "v0.1.3",
            "name": "0.1.3 - stability fixes",
            "body": "- Fixed a UI glitch",
            "html_url": "https://github.com/getonjbghelp/bridgebox/releases/tag/v0.1.3",
        }
    )

    release = await app_update.fetch_latest_release(session)

    assert release.version == "0.1.3"
    assert release.name == "0.1.3 - stability fixes"
    assert release.notes == "- Fixed a UI glitch"
    assert release.critical is False


async def test_fetch_latest_release_flags_a_critical_release():
    session = _FakeSession(
        {
            "tag_name": "v0.1.4",
            "name": "[CRITICAL] 0.1.4",
            "body": "Fixes a token leak - update immediately.",
            "html_url": "https://github.com/getonjbghelp/bridgebox/releases/tag/v0.1.4",
        }
    )

    release = await app_update.fetch_latest_release(session)

    assert release.critical is True


async def test_fetch_latest_release_refuses_an_unexpected_html_url_host():
    session = _FakeSession(
        {
            "tag_name": "v0.1.4",
            "name": "0.1.4",
            "body": "",
            "html_url": "https://evil.example.com/releases/tag/v0.1.4",
        }
    )

    with pytest.raises(ValueError):
        await app_update.fetch_latest_release(session)


# ---- fetch_releases (changelog) --------------------------------------------


async def test_fetch_releases_parses_the_list_newest_first():
    session = _FakeSession(
        [
            {
                "tag_name": "v0.1.6",
                "name": "0.1.6",
                "body": "«Faster warm-up» • MINOR",
                "published_at": "2026-09-01T12:00:00Z",
                "html_url": "https://github.com/getonjbghelp/bridgebox/releases/tag/v0.1.6",
                "draft": False,
            },
            {
                "tag_name": "v0.1.5",
                "name": "0.1.5",
                "body": "old body",
                "published_at": "2026-08-28T09:00:00Z",
                "html_url": "https://github.com/getonjbghelp/bridgebox/releases/tag/v0.1.5",
                "draft": False,
            },
        ]
    )

    releases = await app_update.fetch_releases(session)

    assert [r.version for r in releases] == ["0.1.6", "0.1.5"]
    assert releases[0].body == "«Faster warm-up» • MINOR"
    assert releases[0].date == "2026-09-01"


async def test_fetch_releases_skips_drafts():
    session = _FakeSession(
        [
            {
                "tag_name": "v0.1.7",
                "name": "0.1.7",
                "body": "",
                "published_at": "2026-09-05T00:00:00Z",
                "html_url": "https://github.com/getonjbghelp/bridgebox/releases/tag/v0.1.7",
                "draft": True,
            }
        ]
    )

    releases = await app_update.fetch_releases(session)

    assert releases == []


async def test_fetch_releases_keeps_prereleases():
    """This app ships its normal betas as GitHub releases - excluding
    prereleases would hide most of the actual changelog."""
    session = _FakeSession(
        [
            {
                "tag_name": "v0.1.5",
                "name": "0.1.5 (b1)",
                "body": "",
                "published_at": "2026-08-28T09:00:00Z",
                "html_url": "https://github.com/getonjbghelp/bridgebox/releases/tag/v0.1.5",
                "draft": False,
                "prerelease": True,
            }
        ]
    )

    releases = await app_update.fetch_releases(session)

    assert len(releases) == 1


async def test_fetch_releases_refuses_an_unexpected_html_url_host():
    session = _FakeSession(
        [
            {
                "tag_name": "v0.1.6",
                "name": "0.1.6",
                "body": "",
                "published_at": "2026-09-01T00:00:00Z",
                "html_url": "https://evil.example.com/releases/tag/v0.1.6",
                "draft": False,
            }
        ]
    )

    with pytest.raises(ValueError):
        await app_update.fetch_releases(session)


async def test_fetch_releases_skips_an_entry_with_no_tag():
    session = _FakeSession(
        [
            {
                "tag_name": "",
                "name": "untagged",
                "body": "",
                "published_at": "2026-09-01T00:00:00Z",
                "html_url": "https://github.com/getonjbghelp/bridgebox/releases",
                "draft": False,
            }
        ]
    )

    releases = await app_update.fetch_releases(session)

    assert releases == []


# ---- update asset discovery -------------------------------------------------


async def test_fetch_latest_release_finds_the_portable_zip_asset():
    session = _FakeSession(
        _release_payload(
            assets=[
                {"name": "source.zip", "browser_download_url": "https://github.com/x/source.zip",
                 "size": 10},
                {"name": "BridgeBox_Portable-v0.1.4.zip",
                 "browser_download_url":
                     "https://objects.githubusercontent.com/BridgeBox_Portable-v0.1.4.zip",
                 "size": 60_000_000},
            ]
        )
    )

    release = await app_update.fetch_latest_release(session)

    assert release.asset_url == (
        "https://objects.githubusercontent.com/BridgeBox_Portable-v0.1.4.zip"
    )
    assert release.asset_size == 60_000_000
    assert release.asset_is_archive is True


async def test_fetch_latest_release_captures_the_zip_digest_when_present():
    session = _FakeSession(
        _release_payload(
            assets=[
                {"name": "BridgeBox_Portable.zip",
                 "browser_download_url":
                     "https://objects.githubusercontent.com/BridgeBox_Portable.zip",
                 "digest": "sha256:" + "a" * 64},
            ]
        )
    )

    release = await app_update.fetch_latest_release(session)

    assert release.asset_digest == "sha256:" + "a" * 64


async def test_fetch_latest_release_zip_digest_is_none_on_an_asset_uploaded_before_it_existed():
    session = _FakeSession(
        _release_payload(
            assets=[
                {"name": "BridgeBox_Portable.zip",
                 "browser_download_url":
                     "https://objects.githubusercontent.com/BridgeBox_Portable.zip"},
            ]
        )
    )

    release = await app_update.fetch_latest_release(session)

    assert release.asset_digest is None


async def test_fetch_latest_release_ignores_a_bare_exe_asset_entirely():
    """A onedir install is not runnable from bridgebox.exe alone any more -
    it needs the matching _internal/ folder, which only the portable .zip
    carries (see extract_release_from_archive) - so a bare .exe asset must
    never be picked, whether or not a release also happens to carry one."""
    session = _FakeSession(
        _release_payload(
            assets=[
                {"name": "BridgeBox.exe",
                 "browser_download_url": "https://objects.githubusercontent.com/BridgeBox.exe"},
            ]
        )
    )

    release = await app_update.fetch_latest_release(session)

    assert release.asset_url is None
    assert release.asset_is_archive is False


async def test_fetch_latest_release_picks_the_zip_even_when_a_bare_exe_is_also_offered():
    session = _FakeSession(
        _release_payload(
            assets=[
                {"name": "BridgeBox_Portable.zip",
                 "browser_download_url":
                     "https://objects.githubusercontent.com/BridgeBox_Portable.zip"},
                {"name": "BridgeBox.exe",
                 "browser_download_url":
                     "https://objects.githubusercontent.com/BridgeBox.exe"},
            ]
        )
    )

    release = await app_update.fetch_latest_release(session)

    assert release.asset_url.endswith("BridgeBox_Portable.zip")
    assert release.asset_is_archive is True


async def test_fetch_latest_release_picks_the_portable_zip_among_several():
    """A release that also carries some other archive must not send the
    updater after the wrong one."""
    session = _FakeSession(
        _release_payload(
            assets=[
                {"name": "strategies-backup.zip",
                 "browser_download_url":
                     "https://objects.githubusercontent.com/strategies-backup.zip"},
                {"name": "BridgeBox_Portable-v0.1.4.zip",
                 "browser_download_url":
                     "https://objects.githubusercontent.com/BridgeBox_Portable-v0.1.4.zip"},
            ]
        )
    )

    release = await app_update.fetch_latest_release(session)

    assert release.asset_url.endswith("BridgeBox_Portable-v0.1.4.zip")


async def test_fetch_latest_release_has_no_asset_url_when_nothing_is_downloadable():
    session = _FakeSession(_release_payload(assets=[{"name": "notes.txt"}]))

    release = await app_update.fetch_latest_release(session)

    assert release.asset_url is None
    assert release.asset_size == 0
    assert release.asset_is_archive is False


async def test_fetch_latest_release_refuses_a_zip_asset_on_an_unexpected_host():
    session = _FakeSession(
        _release_payload(
            assets=[
                {"name": "BridgeBox_Portable.zip",
                 "browser_download_url": "https://evil.example.com/BridgeBox_Portable.zip"},
            ]
        )
    )

    with pytest.raises(ValueError):
        await app_update.fetch_latest_release(session)


# ---- download_exe resilience ------------------------------------------------


class _FlakyDownload:
    def __init__(self, failures: int, body: bytes = b"exe-bytes", exc=None):
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
            url = "https://objects.githubusercontent.com/BridgeBox.exe"
            headers = {"Content-Length": str(len(body))}
            content = _Content()

            def raise_for_status(self):
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        return _Response()


async def test_download_exe_retries_a_dropped_connection_then_succeeds(tmp_path: Path):
    session = _FlakyDownload(failures=2)
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    out = await app_update.download_exe(
        session, "https://objects.githubusercontent.com/BridgeBox.exe",
        tmp_path / "BridgeBox.exe.new", sleep=fake_sleep,
    )

    assert out.read_bytes() == b"exe-bytes"
    assert session.calls == 3


class _TruncatingDownload:
    """A session whose Content-Length lies for the first `truncations` calls -
    the body actually delivered is shorter than what the header promised, the
    same shape a connection cut exactly on a chunk boundary produces."""

    def __init__(self, truncations: int, full_body: bytes = b"exe-bytes-full"):
        self.truncations = truncations
        self.full_body = full_body
        self.calls = 0

    def get(self, url):
        self.calls += 1
        truncated = self.calls <= self.truncations
        body = self.full_body[:-3] if truncated else self.full_body
        announced_length = len(self.full_body)

        class _Content:
            async def iter_chunked(self, _size):
                yield body

        class _Response:
            status = 200
            url = "https://objects.githubusercontent.com/BridgeBox.exe"
            headers = {"Content-Length": str(announced_length)}
            content = _Content()

            def raise_for_status(self):
                return None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        return _Response()


async def test_download_exe_retries_a_truncated_response_then_succeeds(tmp_path: Path):
    session = _TruncatingDownload(truncations=1)
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    out = await app_update.download_exe(
        session, "https://objects.githubusercontent.com/BridgeBox.exe",
        tmp_path / "BridgeBox.exe.new", sleep=fake_sleep,
    )

    assert out.read_bytes() == session.full_body
    assert session.calls == 2
    assert slept == [app_update.DOWNLOAD_RETRY_DELAY_S]


async def test_download_exe_gives_up_on_a_persistently_truncated_response(tmp_path: Path):
    session = _TruncatingDownload(truncations=999)
    dest = tmp_path / "BridgeBox.exe.new"

    async def fake_sleep(_seconds):
        return None

    with pytest.raises(app_update.IncompleteDownload):
        await app_update.download_exe(session, "https://objects.githubusercontent.com/BridgeBox.exe",
                                       dest, sleep=fake_sleep)

    assert not dest.exists(), "a truncated exe must never be left as if it downloaded cleanly"


async def test_download_exe_refuses_an_oversized_response():
    session = _FlakyDownload(failures=0, body=b"x" * 128)

    with pytest.raises(ValueError, match="refusing"):
        await app_update.download_exe(
            session, "https://objects.githubusercontent.com/BridgeBox.exe",
            Path("unused.exe"), max_bytes=64,
        )


def _write_dir(path: Path, files: dict[str, bytes]) -> Path:
    for name, data in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return path


# ---- cleanup_stale_files -----------------------------------------------------


def test_cleanup_stale_files_removes_both_a_leftover_backup_and_stage_file(tmp_path: Path):
    current = tmp_path / "BridgeBox.exe"
    current.write_bytes(b"current")
    (tmp_path / "BridgeBox.exe.old").write_bytes(b"stale-backup")
    (tmp_path / "BridgeBox.exe.new").write_bytes(b"stale-stage")

    swapped = app_update.cleanup_stale_files(current)

    assert not (tmp_path / "BridgeBox.exe.old").exists()
    assert not (tmp_path / "BridgeBox.exe.new").exists()
    assert swapped is True


def test_cleanup_stale_files_also_removes_a_leftover_internal_backup_and_stage_dir(
    tmp_path: Path,
):
    """The _internal/ sibling gets the same treatment as the exe - both
    halves of a onedir install can leave a stray .old/.new behind."""
    current = tmp_path / "BridgeBox.exe"
    current.write_bytes(b"current")
    (tmp_path / "_internal.old" / "base_library.zip").parent.mkdir()
    (tmp_path / "_internal.old" / "base_library.zip").write_bytes(b"stale-backup")
    (tmp_path / "_internal.new" / "base_library.zip").parent.mkdir()
    (tmp_path / "_internal.new" / "base_library.zip").write_bytes(b"stale-stage")

    swapped = app_update.cleanup_stale_files(current)

    assert not (tmp_path / "_internal.old").exists()
    assert not (tmp_path / "_internal.new").exists()
    assert swapped is True


def test_cleanup_stale_files_is_a_silent_noop_when_nothing_is_there(tmp_path: Path):
    swapped = app_update.cleanup_stale_files(tmp_path / "BridgeBox.exe")  # must not raise
    assert swapped is False


def test_cleanup_stale_files_reports_no_swap_when_only_a_stage_file_is_left(tmp_path: Path):
    """A leftover .new with no matching .old means a download never got as
    far as the relaunch script touching anything real - main() must not
    re-baseline integrity over a swap that never happened."""
    current = tmp_path / "BridgeBox.exe"
    current.write_bytes(b"current")
    (tmp_path / "BridgeBox.exe.new").write_bytes(b"interrupted-download")

    swapped = app_update.cleanup_stale_files(current)

    assert swapped is False


# ---- build_relaunch_script ---------------------------------------------------


def _script_paths(tmp_path: Path):
    return dict(
        pid=4242,
        exe_path=tmp_path / "BridgeBox.exe",
        exe_stage=tmp_path / "BridgeBox.exe.new",
        internal_path=tmp_path / "_internal",
        internal_stage=tmp_path / "_internal.new",
    )


def test_build_relaunch_script_waits_for_the_given_pid(tmp_path: Path):
    script = app_update.build_relaunch_script(**_script_paths(tmp_path))
    assert "Wait-Process -Id 4242" in script
    assert f"-Timeout {app_update.RELAUNCH_EXIT_TIMEOUT_S}" in script


def test_build_relaunch_script_grants_permissions_on_both_paths_before_any_move(tmp_path: Path):
    paths = _script_paths(tmp_path)
    script = app_update.build_relaunch_script(**paths)

    icacls_line = f'icacls "{paths["internal_path"]}"'
    first_move = "call :move_retry"
    assert script.index(icacls_line) < script.index(first_move), (
        "an icacls grant must run before any rename is attempted, same "
        "ordering zapret/update.py's own install_release uses"
    )
    assert app_update.SID_ADMINISTRATORS in script
    assert app_update.SID_SYSTEM in script


def test_build_relaunch_script_swaps_internal_before_the_exe(tmp_path: Path):
    """_internal/ is the half that was actually failing in the wild - a
    failure there must leave the exe untouched, so it has to be attempted
    first."""
    paths = _script_paths(tmp_path)
    script = app_update.build_relaunch_script(**paths)

    internal_move = f'call :move_retry "{paths["internal_path"]}"'
    exe_move = f'call :move_retry "{paths["exe_path"]}"'
    assert script.index(internal_move) < script.index(exe_move)


def test_build_relaunch_script_starts_the_new_exe_and_deletes_itself(tmp_path: Path):
    paths = _script_paths(tmp_path)
    script = app_update.build_relaunch_script(**paths)

    assert f'start "" "{paths["exe_path"]}"' in script
    assert 'del "%~f0"' in script


def test_build_relaunch_script_rolls_back_internal_if_the_exe_swap_fails(tmp_path: Path):
    """Both halves must land or neither does - the one pairing this app
    cannot start with (a new exe against an old _internal/, or vice versa)
    must never be left behind by a partial failure."""
    paths = _script_paths(tmp_path)
    script = app_update.build_relaunch_script(**paths)

    # The exe-swap failure branch must move _internal/ back to its own stage
    # path and its backup back to the live path - both moves the successful
    # path already did, undone.
    rollback_stage = f'call :move_retry "{paths["internal_path"]}" "{paths["internal_stage"]}"'
    rollback_restore = (
        f'call :move_retry "{paths["internal_path"]}.old" "{paths["internal_path"]}"'
    )
    assert rollback_stage in script
    assert rollback_restore in script


# ---- verify_exe_digest -----------------------------------------------------


def test_verify_exe_digest_accepts_a_matching_sha256(tmp_path: Path):
    path = tmp_path / "BridgeBox.exe"
    path.write_bytes(b"hello world")
    import hashlib

    digest = "sha256:" + hashlib.sha256(b"hello world").hexdigest()

    app_update.verify_exe_digest(path, digest)  # must not raise


def test_verify_exe_digest_refuses_a_mismatch(tmp_path: Path):
    path = tmp_path / "BridgeBox.exe"
    path.write_bytes(b"tampered or truncated")

    with pytest.raises(ValueError, match="checksum"):
        app_update.verify_exe_digest(path, "sha256:" + "0" * 64)


def test_verify_exe_digest_is_case_insensitive():
    path_holder = {}

    import hashlib
    import tempfile
    from pathlib import Path as _Path

    with tempfile.TemporaryDirectory() as tmp:
        path = _Path(tmp) / "x.exe"
        path.write_bytes(b"payload")
        digest = "sha256:" + hashlib.sha256(b"payload").hexdigest().upper()
        path_holder["ok"] = True
        app_update.verify_exe_digest(path, digest)  # must not raise

    assert path_holder["ok"]


def test_verify_exe_digest_allows_a_release_with_no_digest(tmp_path: Path):
    path = tmp_path / "BridgeBox.exe"
    path.write_bytes(b"anything")

    app_update.verify_exe_digest(path, None)  # must not raise - nothing to check


def test_verify_exe_digest_rejects_an_unrecognised_algorithm(tmp_path: Path):
    path = tmp_path / "BridgeBox.exe"
    path.write_bytes(b"anything")

    with pytest.raises(ValueError, match="unrecognised"):
        app_update.verify_exe_digest(path, "md5:deadbeef")


def test_running_exe_path_is_none_unless_frozen(monkeypatch):
    monkeypatch.delattr(app_update.sys, "frozen", raising=False)
    assert app_update.running_exe_path() is None


def test_running_exe_path_is_sys_executable_when_frozen(monkeypatch):
    monkeypatch.setattr(app_update.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_update.sys, "executable", r"C:\Portable\BridgeBox.exe", raising=False)
    assert app_update.running_exe_path() == Path(r"C:\Portable\BridgeBox.exe")


def test_running_internal_dir_is_none_unless_frozen(monkeypatch):
    monkeypatch.delattr(app_update.sys, "frozen", raising=False)
    assert app_update.running_internal_dir() is None


def test_running_internal_dir_sits_beside_the_running_exe_when_frozen(monkeypatch):
    monkeypatch.setattr(app_update.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_update.sys, "executable", r"C:\Portable\BridgeBox.exe", raising=False)
    assert app_update.running_internal_dir() == Path(r"C:\Portable\_internal")


def test_running_internal_dir_is_returned_even_when_it_does_not_exist_yet(
    monkeypatch, tmp_path: Path
):
    """The one real case this covers: an old onefile install (no _internal/
    at all) updating straight to the first onedir release - the path is
    still a valid swap target, just one with nothing at it yet."""
    monkeypatch.setattr(app_update.sys, "frozen", True, raising=False)
    monkeypatch.setattr(app_update.sys, "executable", str(tmp_path / "BridgeBox.exe"), raising=False)
    assert not (tmp_path / "_internal").exists()
    assert app_update.running_internal_dir() == tmp_path / "_internal"


def test_stage_path_for_appends_the_new_suffix():
    assert app_update.stage_path_for(Path("BridgeBox.exe")) == Path("BridgeBox.exe.new")


# ---- extract_release_from_archive -------------------------------------------


def _portable_zip(path: Path, *, members: dict[str, bytes]) -> Path:
    import zipfile

    with zipfile.ZipFile(path, "w") as bundle:
        for name, data in members.items():
            bundle.writestr(name, data)
    return path


def _portable_release_members(**extra: bytes) -> dict[str, bytes]:
    """A minimal but realistic portable release archive's contents - the
    root folder name carries the version, same as a real one - plus
    whatever extra members a test wants layered on top."""
    return {
        "BridgeBox_Portable-v0.1.4/README.md": b"# readme",
        "BridgeBox_Portable-v0.1.4/bridgebox.exe": b"MZ-exe-payload",
        "BridgeBox_Portable-v0.1.4/_internal/base_library.zip": b"internal-payload",
        "BridgeBox_Portable-v0.1.4/zapret/winws.exe": b"not-touched-by-self-update",
        "BridgeBox_Portable-v0.1.4/config.yaml": b"not-touched-either",
        **{f"BridgeBox_Portable-v0.1.4/{name}": data for name, data in extra.items()},
    }


def test_extract_release_pulls_the_exe_and_internal_folder_nested_under_the_release_folder(
    tmp_path: Path,
):
    """The release archive nests everything one level down, and that folder
    name carries the version - so the exe is found by name at any depth,
    never by a hard-coded path, and _internal/'s members are found relative
    to wherever the exe turned out to be."""
    archive = _portable_zip(
        tmp_path / "BridgeBox_Portable-v0.1.4.zip",
        members=_portable_release_members(**{"_internal/sub/extra.pyd": b"nested-payload"}),
    )
    exe_dest = tmp_path / "staged.exe"
    internal_dest = tmp_path / "staged_internal"

    result = app_update.extract_release_from_archive(archive, exe_dest, internal_dest)

    assert result == exe_dest
    assert exe_dest.read_bytes() == b"MZ-exe-payload"
    assert (internal_dest / "base_library.zip").read_bytes() == b"internal-payload"
    assert (internal_dest / "sub" / "extra.pyd").read_bytes() == b"nested-payload"
    # zapret/ and config.yaml are the release's own, never self-update's to touch.
    assert not (internal_dest / "zapret").exists()
    assert not (tmp_path / "config.yaml").exists()


def test_extract_release_raises_when_the_archive_holds_no_bridgebox_exe(tmp_path: Path):
    archive = _portable_zip(
        tmp_path / "wrong.zip", members={"docs/readme.txt": b"nothing here"}
    )

    with pytest.raises(RuntimeError, match="no bridgebox.exe"):
        app_update.extract_release_from_archive(
            archive, tmp_path / "staged.exe", tmp_path / "staged_internal"
        )


def test_extract_release_raises_when_the_archive_has_no_internal_folder(tmp_path: Path):
    """A bare .exe-only archive is not a onedir portable release - refused
    rather than silently accepted and left half-extracted."""
    archive = _portable_zip(
        tmp_path / "exe-only.zip",
        members={"BridgeBox_Portable-v0.1.4/bridgebox.exe": b"MZ-exe-payload"},
    )

    with pytest.raises(RuntimeError, match="_internal"):
        app_update.extract_release_from_archive(
            archive, tmp_path / "staged.exe", tmp_path / "staged_internal"
        )


def test_extract_release_ignores_a_traversal_path_in_the_archive(tmp_path: Path):
    """A hostile entry name must not steer where anything lands: every
    member goes to a path computed from its OWN name relative to the exe's,
    never the archive's own stored (possibly ../../-laden) one."""
    archive = _portable_zip(
        tmp_path / "evil.zip",
        members={
            "../../../../bridgebox.exe": b"payload",
            "../../../../_internal/base_library.zip": b"internal-payload",
        },
    )
    exe_dest = tmp_path / "staged" / "staged.exe"
    internal_dest = tmp_path / "staged" / "staged_internal"

    app_update.extract_release_from_archive(archive, exe_dest, internal_dest)

    assert exe_dest.read_bytes() == b"payload"
    assert (internal_dest / "base_library.zip").read_bytes() == b"internal-payload"
    # Nothing was written outside the directories we named.
    assert not (tmp_path.parent / "bridgebox.exe").exists()


def test_extract_release_refuses_a_traversal_hidden_after_the_internal_prefix(
    tmp_path: Path,
):
    """The traversal test above uses ".." BEFORE release_root/_internal - the
    one shape relative_to()+the INTERNAL_DIR_NAME check actually rule out,
    since both only look at the first path segment. ".." AFTER that prefix
    (still matching both checks) used to sail straight through: joinpath()
    does not resolve ".." the way os.path.normpath would, so the member
    below used to land outside internal_dest entirely instead of being
    refused."""
    archive = _portable_zip(
        tmp_path / "BridgeBox_Portable-v0.1.4.zip",
        members=_portable_release_members(
            **{"_internal/../../../../evil.exe": b"escaped-payload"}
        ),
    )
    exe_dest = tmp_path / "staged" / "staged.exe"
    internal_dest = tmp_path / "staged" / "staged_internal"

    with pytest.raises(ValueError, match="escapes the extraction root"):
        app_update.extract_release_from_archive(archive, exe_dest, internal_dest)

    # A refusal cleans up, same as the zip-bomb refusal below - a half
    # extracted, unverified self-update must not be left in place. Before the
    # fix, nothing here raised at all: the member silently wrote past
    # internal_dest and extraction reported success, which is the whole bug -
    # so pytest.raises above is already the real regression guard.
    assert not exe_dest.exists()
    assert not internal_dest.exists()


def test_extract_release_refuses_a_zip_bomb(tmp_path: Path):
    """The cap is measured on the decompressed stream, not on any entry's
    own declared size - a lying header must not buy unbounded disk. Applies
    across the whole extraction (exe + _internal/ together), and a refusal
    cleans up whatever had already been written."""
    archive = _portable_zip(
        tmp_path / "bomb.zip",
        members=_portable_release_members(**{"_internal/huge.bin": b"\0" * 5_000_000}),
    )
    exe_dest = tmp_path / "staged.exe"
    internal_dest = tmp_path / "staged_internal"

    with pytest.raises(ValueError, match="exceeds"):
        app_update.extract_release_from_archive(
            archive, exe_dest, internal_dest, max_bytes=1_000_000
        )

    assert not exe_dest.exists()
    assert not internal_dest.exists()


# ---- synthetic end-to-end: real HTTP, not a mocked session ------------------


def _release_server_handler(routes: dict[str, tuple[bytes, str]]):
    """A minimal http.server handler serving whatever `routes` holds - a
    factory rather than a bare class, since BaseHTTPRequestHandler wants its
    behaviour on the class itself and this needs a fresh one per test,
    closing over that test's own `routes` dict."""
    import http.server

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            entry = routes.get(self.path)
            if entry is None:
                self.send_response(404)
                self.end_headers()
                return
            body, content_type = entry
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002 - stdlib's own name
            pass  # keep test output free of a request log line per fetch

    return Handler


@pytest.fixture
def release_server():
    """A real ThreadingHTTPServer on a free localhost port, torn down at the
    end of the test. Yields (base_url, routes) - a test fills `routes` in
    (path -> (body_bytes, content_type)) before making requests."""
    import http.server
    import threading

    routes: dict[str, tuple[bytes, str]] = {}
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _release_server_handler(routes))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", routes
    finally:
        server.shutdown()
        thread.join(timeout=5)


async def test_self_update_pipeline_end_to_end_against_a_real_http_server(
    tmp_path: Path, release_server
):
    """The rest of this file drives fetch_latest_release/download_exe
    against a hand-mocked session, which proves the parsing and retry logic
    but never actually opens a socket. This drives the same two functions
    against a REAL local server instead - real DNS-free connect, real
    chunked streaming, a real Content-Length - through to a real digest
    check and a real extraction, the same sequence
    Api._apply_app_update_coro runs in production. GitHub itself is
    obviously not reachable from a test, so this uses the test-only
    allowed_hosts/require_https override _require_allowed_host exists for -
    every production call site keeps the real github.com/https-only check."""
    import hashlib
    import json

    import aiohttp

    base_url, routes = release_server

    archive_src = tmp_path / "source.zip"
    _portable_zip(archive_src, members=_portable_release_members())
    zip_bytes = archive_src.read_bytes()
    digest = "sha256:" + hashlib.sha256(zip_bytes).hexdigest()

    routes["/download/BridgeBox_Portable.zip"] = (zip_bytes, "application/zip")
    payload = {
        "tag_name": "v9.9.9",
        "name": "9.9.9",
        "body": "synthetic test release",
        "html_url": f"{base_url}/releases/tag/v9.9.9",
        "assets": [
            {
                "name": "BridgeBox_Portable.zip",
                "browser_download_url": f"{base_url}/download/BridgeBox_Portable.zip",
                "size": len(zip_bytes),
                "digest": digest,
            }
        ],
    }
    routes["/releases/latest"] = (json.dumps(payload).encode("utf-8"), "application/json")

    local_only = frozenset({"127.0.0.1"})
    async with aiohttp.ClientSession() as session:
        release = await app_update.fetch_latest_release(
            session,
            url=f"{base_url}/releases/latest",
            allowed_hosts=local_only,
            require_https=False,
        )
        assert release.version == "9.9.9"
        assert release.asset_is_archive is True
        assert release.asset_digest == digest

        downloaded = tmp_path / "downloaded.zip"
        progress_calls: list[tuple[int, int]] = []
        await app_update.download_exe(
            session, release.asset_url, downloaded, allowed_hosts=local_only, require_https=False,
            on_progress=lambda received, total: progress_calls.append((received, total)),
        )

    assert downloaded.read_bytes() == zip_bytes
    # on_progress is what api/app_update.py's _apply_app_update_coro feeds
    # into _app_apply_state for the UI's progress bar - a real server with a
    # real Content-Length is what actually proves the byte counts, not a
    # hand-mocked session that could report anything.
    assert progress_calls, "on_progress must fire at least once for a real download"
    assert progress_calls[-1] == (len(zip_bytes), len(zip_bytes))

    app_update.verify_exe_digest(downloaded, release.asset_digest)  # must not raise

    exe_stage = tmp_path / "staged.exe"
    internal_stage = tmp_path / "staged_internal"
    result = app_update.extract_release_from_archive(downloaded, exe_stage, internal_stage)

    assert result == exe_stage
    assert exe_stage.read_bytes() == b"MZ-exe-payload"
    assert (internal_stage / "base_library.zip").read_bytes() == b"internal-payload"
    # zapret/ and config.yaml are the release's own, never self-update's to touch.
    assert not (internal_stage / "zapret").exists()
    assert not (tmp_path / "config.yaml").exists()


async def test_self_update_pipeline_end_to_end_refuses_a_tampered_download(
    tmp_path: Path, release_server
):
    """Same real server, but the bytes actually served do not match the
    digest GitHub "reported" - the one failure mode this whole pipeline
    exists to catch (see verify_exe_digest's own docstring: a truncated or
    mangled-in-transit download looks identical to a clean one on the wire)."""
    import hashlib
    import json

    import aiohttp

    base_url, routes = release_server

    archive_src = tmp_path / "source.zip"
    _portable_zip(archive_src, members=_portable_release_members())
    real_bytes = archive_src.read_bytes()
    wrong_digest = "sha256:" + hashlib.sha256(b"not what gets served").hexdigest()

    routes["/download/BridgeBox_Portable.zip"] = (real_bytes, "application/zip")
    payload = {
        "tag_name": "v9.9.9",
        "html_url": f"{base_url}/releases/tag/v9.9.9",
        "assets": [
            {
                "name": "BridgeBox_Portable.zip",
                "browser_download_url": f"{base_url}/download/BridgeBox_Portable.zip",
                "size": len(real_bytes),
                "digest": wrong_digest,
            }
        ],
    }
    routes["/releases/latest"] = (json.dumps(payload).encode("utf-8"), "application/json")

    local_only = frozenset({"127.0.0.1"})
    async with aiohttp.ClientSession() as session:
        release = await app_update.fetch_latest_release(
            session,
            url=f"{base_url}/releases/latest",
            allowed_hosts=local_only,
            require_https=False,
        )
        downloaded = tmp_path / "downloaded.zip"
        await app_update.download_exe(
            session, release.asset_url, downloaded, allowed_hosts=local_only, require_https=False,
        )

    with pytest.raises(ValueError, match="checksum"):
        app_update.verify_exe_digest(downloaded, release.asset_digest)
