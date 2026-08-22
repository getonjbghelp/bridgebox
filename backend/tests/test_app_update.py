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


# ---- exe asset discovery ---------------------------------------------------


async def test_fetch_latest_release_finds_the_exe_asset():
    session = _FakeSession(
        _release_payload(
            assets=[
                {"name": "source.zip", "browser_download_url": "https://github.com/x/source.zip",
                 "size": 10},
                {"name": "BridgeBox.exe",
                 "browser_download_url": "https://objects.githubusercontent.com/BridgeBox.exe",
                 "size": 42_000_000},
            ]
        )
    )

    release = await app_update.fetch_latest_release(session)

    assert release.asset_url == "https://objects.githubusercontent.com/BridgeBox.exe"
    assert release.asset_size == 42_000_000


async def test_fetch_latest_release_captures_the_exe_digest_when_present():
    session = _FakeSession(
        _release_payload(
            assets=[
                {"name": "BridgeBox.exe",
                 "browser_download_url": "https://objects.githubusercontent.com/BridgeBox.exe",
                 "digest": "sha256:" + "a" * 64},
            ]
        )
    )

    release = await app_update.fetch_latest_release(session)

    assert release.asset_digest == "sha256:" + "a" * 64


async def test_fetch_latest_release_exe_digest_is_none_on_an_asset_uploaded_before_it_existed():
    session = _FakeSession(
        _release_payload(
            assets=[
                {"name": "BridgeBox.exe",
                 "browser_download_url": "https://objects.githubusercontent.com/BridgeBox.exe"},
            ]
        )
    )

    release = await app_update.fetch_latest_release(session)

    assert release.asset_digest is None


async def test_fetch_latest_release_falls_back_to_the_portable_zip():
    """Releases ship the portable .zip, not a bare .exe - the exe alone is
    not a runnable BridgeBox (it needs zapret/ beside it), so a release
    with no .exe asset is the normal case, not a broken one."""
    session = _FakeSession(
        _release_payload(
            assets=[
                {"name": "BridgeBox_Portable-v0.1.4.zip",
                 "browser_download_url":
                     "https://objects.githubusercontent.com/BridgeBox_Portable-v0.1.4.zip",
                 "size": 60_000_000,
                 "digest": "sha256:" + "b" * 64},
            ]
        )
    )

    release = await app_update.fetch_latest_release(session)

    assert release.asset_url.endswith("BridgeBox_Portable-v0.1.4.zip")
    assert release.asset_is_archive is True
    assert release.asset_digest == "sha256:" + "b" * 64


async def test_fetch_latest_release_prefers_a_bare_exe_over_the_zip():
    """A .exe needs no unpacking, so it wins when a release carries both."""
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

    assert release.asset_url.endswith("BridgeBox.exe")
    assert release.asset_is_archive is False


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


async def test_fetch_latest_release_refuses_an_exe_asset_on_an_unexpected_host():
    session = _FakeSession(
        _release_payload(
            assets=[
                {"name": "BridgeBox.exe",
                 "browser_download_url": "https://evil.example.com/BridgeBox.exe"},
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


# ---- replace_running_exe / cleanup_stale_files -----------------------------


def test_replace_running_exe_swaps_the_file_and_returns_the_backup_path(tmp_path: Path):
    current = tmp_path / "BridgeBox.exe"
    current.write_bytes(b"old")
    new = tmp_path / "BridgeBox.exe.new"
    new.write_bytes(b"new")

    backup = app_update.replace_running_exe(new, current)

    assert current.read_bytes() == b"new"
    assert backup == tmp_path / "BridgeBox.exe.old"
    assert backup.read_bytes() == b"old"
    assert not new.exists()


def test_replace_running_exe_overwrites_a_leftover_backup_from_a_prior_update(tmp_path: Path):
    current = tmp_path / "BridgeBox.exe"
    current.write_bytes(b"old")
    (tmp_path / "BridgeBox.exe.old").write_bytes(b"stale-from-last-time")
    new = tmp_path / "BridgeBox.exe.new"
    new.write_bytes(b"new")

    app_update.replace_running_exe(new, current)

    assert (tmp_path / "BridgeBox.exe.old").read_bytes() == b"old"


def test_replace_running_exe_rolls_back_if_the_final_move_fails(tmp_path: Path, monkeypatch):
    current = tmp_path / "BridgeBox.exe"
    current.write_bytes(b"old")
    new = tmp_path / "BridgeBox.exe.new"
    new.write_bytes(b"new")

    import os as os_module

    real_replace = os_module.replace
    calls = []

    def flaky_replace(src, dst):
        calls.append((str(src), str(dst)))
        if str(src) == str(new):
            raise OSError("simulated failure moving the new exe into place")
        return real_replace(src, dst)

    monkeypatch.setattr(app_update.os, "replace", flaky_replace)

    with pytest.raises(OSError):
        app_update.replace_running_exe(new, current)

    assert current.read_bytes() == b"old", "must still be launchable after a failed swap"


def test_replace_running_exe_retries_a_locked_rename_then_succeeds(tmp_path: Path, monkeypatch):
    current = tmp_path / "BridgeBox.exe"
    current.write_bytes(b"old")
    new = tmp_path / "BridgeBox.exe.new"
    new.write_bytes(b"new")

    import os as os_module

    real_replace = os_module.replace
    calls = {"n": 0}

    def locked_once_replace(src, dst):
        if str(src) == str(new) and calls["n"] == 0:
            calls["n"] += 1
            exc = OSError("locked by an antivirus scan")
            exc.winerror = 5
            raise exc
        return real_replace(src, dst)

    monkeypatch.setattr(app_update.os, "replace", locked_once_replace)
    slept = []

    app_update.replace_running_exe(new, current, sleep=slept.append)

    assert current.read_bytes() == b"new"
    assert slept == [app_update.REPLACE_RETRY_DELAY_S]


def test_replace_running_exe_gives_up_after_the_retry_budget_on_a_persistent_lock(
    tmp_path: Path, monkeypatch
):
    current = tmp_path / "BridgeBox.exe"
    current.write_bytes(b"old")
    new = tmp_path / "BridgeBox.exe.new"
    new.write_bytes(b"new")

    import os as os_module

    real_replace = os_module.replace

    def always_locked_replace(src, dst):
        if str(src) == str(new):
            exc = OSError("still locked")
            exc.winerror = 32
            raise exc
        return real_replace(src, dst)

    monkeypatch.setattr(app_update.os, "replace", always_locked_replace)

    with pytest.raises(OSError):
        app_update.replace_running_exe(new, current, sleep=lambda _s: None)

    assert current.read_bytes() == b"old", "must still be launchable after giving up"


def test_cleanup_stale_files_removes_both_a_leftover_backup_and_stage_file(tmp_path: Path):
    current = tmp_path / "BridgeBox.exe"
    current.write_bytes(b"current")
    (tmp_path / "BridgeBox.exe.old").write_bytes(b"stale-backup")
    (tmp_path / "BridgeBox.exe.new").write_bytes(b"stale-stage")

    app_update.cleanup_stale_files(current)

    assert not (tmp_path / "BridgeBox.exe.old").exists()
    assert not (tmp_path / "BridgeBox.exe.new").exists()


def test_cleanup_stale_files_is_a_silent_noop_when_nothing_is_there(tmp_path: Path):
    app_update.cleanup_stale_files(tmp_path / "BridgeBox.exe")  # must not raise


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


def test_stage_path_for_appends_the_new_suffix():
    assert app_update.stage_path_for(Path("BridgeBox.exe")) == Path("BridgeBox.exe.new")


# ---- extract_exe_from_archive ----------------------------------------------


def _portable_zip(path: Path, *, members: dict[str, bytes]) -> Path:
    import zipfile

    with zipfile.ZipFile(path, "w") as bundle:
        for name, data in members.items():
            bundle.writestr(name, data)
    return path


def test_extract_exe_finds_the_exe_nested_under_the_release_folder(tmp_path: Path):
    """The release archive nests everything one level down, and that folder
    name carries the version - so the exe is found by name at any depth,
    never by a hard-coded path."""
    archive = _portable_zip(
        tmp_path / "BridgeBox_Portable-v0.1.4.zip",
        members={
            "BridgeBox_Portable-v0.1.4/README.md": b"# readme",
            "BridgeBox_Portable-v0.1.4/bridgebox.exe": b"MZ-real-payload",
            "BridgeBox_Portable-v0.1.4/zapret/winws.exe": b"not-the-app",
        },
    )
    dest = tmp_path / "staged.exe"

    result = app_update.extract_exe_from_archive(archive, dest)

    assert result == dest
    assert dest.read_bytes() == b"MZ-real-payload"


def test_extract_exe_raises_when_the_archive_holds_no_bridgebox_exe(tmp_path: Path):
    archive = _portable_zip(
        tmp_path / "wrong.zip", members={"docs/readme.txt": b"nothing here"}
    )

    with pytest.raises(RuntimeError, match="no bridgebox.exe"):
        app_update.extract_exe_from_archive(archive, tmp_path / "staged.exe")


def test_extract_exe_ignores_a_traversal_path_in_the_archive(tmp_path: Path):
    """A hostile entry name must not steer where anything lands: exactly one
    member is read, and it goes to the path this function was handed."""
    archive = _portable_zip(
        tmp_path / "evil.zip",
        members={"../../../../bridgebox.exe": b"payload"},
    )
    dest = tmp_path / "staged" / "staged.exe"

    app_update.extract_exe_from_archive(archive, dest)

    assert dest.read_bytes() == b"payload"
    # Nothing was written outside the directory we named.
    assert not (tmp_path.parent / "bridgebox.exe").exists()


def test_extract_exe_refuses_a_zip_bomb(tmp_path: Path):
    """The cap is measured on the decompressed stream, not on the entry's
    own declared size - a lying header must not buy unbounded disk."""
    archive = _portable_zip(
        tmp_path / "bomb.zip", members={"bridgebox.exe": b"\0" * 5_000_000}
    )
    dest = tmp_path / "staged.exe"

    with pytest.raises(ValueError, match="exceeds"):
        app_update.extract_exe_from_archive(archive, dest, max_bytes=1_000_000)

    assert not dest.exists()
