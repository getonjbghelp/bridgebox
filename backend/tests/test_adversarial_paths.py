"""Regression tests for path/encoding/integrity defects an adversarial audit
found in the self-updater (1 and 2 now fixed; 3 is documented, intentional
behavior whose risk is closed by finding 2's fix instead):

1. build_relaunch_script() + restart_after_app_update() used to write the
   helper .bat as UTF-8 with no BOM and no `chcp`. cmd.exe reads a batch file
   in the machine's OEM code page (cp866 on a Russian Windows, cp1252 on a
   Western one), NOT UTF-8 - so every `move` / `rmdir` line naming the
   install directory got a mojibake path the moment that directory contained
   a non-ASCII character, the common case for this app's Russian userbase (a
   user profile folder named in Cyrillic). The swap - including a [critical]
   security update - then silently failed. Fixed: the script now runs
   `chcp 65001` before anything else, and restart_after_app_update writes it
   with `encoding="utf-8-sig"` (a BOM) - the combination cmd.exe needs to
   decode a UTF-8 batch file instead of the OEM code page.

2. download_exe() only enforced "did I receive all the promised bytes" when
   the server sent a Content-Length - a response with no Content-Length that
   ended early was accepted as a complete download. Fixed: download_exe now
   takes `expected_size` (AppRelease.asset_size, from GitHub's Releases API
   itself, independent of this specific request's headers) and falls back to
   it when Content-Length is absent; the caller passes release.asset_size.

3. verify_exe_digest(path, None) still returns without checking anything -
   unchanged, and still correct: GitHub only started attaching asset digests
   in mid-2025, and a release with none is meant to be let through rather
   than blocked forever. What made this a real risk was finding 2 (a
   truncated file with no digest sailing through both gates) - now that
   download_exe checks length even without a Content-Length header, an
   attacker can no longer combine the two.
"""
from __future__ import annotations

import asyncio
from pathlib import PureWindowsPath

import pytest

from bridgebox import app_update


# --------------------------------------------------------------------------
# 1. relaunch .bat encoding vs. cmd.exe's OEM code page
# --------------------------------------------------------------------------


def _script_for(install_dir: str) -> str:
    exe = PureWindowsPath(install_dir) / "bridgebox.exe"
    internal = PureWindowsPath(install_dir) / "_internal"
    return app_update.build_relaunch_script(
        pid=4242,
        exe_path=exe,
        exe_stage=exe.with_name("bridgebox.exe.new"),
        internal_path=internal,
        internal_stage=internal.with_name("_internal.new"),
    )


@pytest.mark.parametrize("oem_cp", ["cp866", "cp1251", "cp850"])
def test_relaunch_script_no_longer_needs_the_oem_codepage_at_all(oem_cp):
    """Regression: restart_after_app_update now writes this text with
    encoding='utf-8-sig' (a BOM), so cmd.exe no longer falls back to
    decoding it with the machine's OEM code page in the first place - which
    of the three does not matter any more."""
    install_dir = r"C:\Users\Пользователь\BridgeBox"
    script = _script_for(install_dir)

    on_disk = script.encode("utf-8-sig")  # exactly what api/app_update.py writes now
    assert on_disk.startswith(b"\xef\xbb\xbf")  # the BOM that makes cmd.exe use UTF-8
    # chcp must switch the console code page before any move/rmdir line that
    # embeds the (possibly Cyrillic) install path runs.
    assert script.index("chcp 65001") < script.index(install_dir.split("\\")[-2])


def test_relaunch_script_now_has_a_bom_and_a_chcp_to_make_utf8_safe():
    script = _script_for(r"C:\Games\BridgeBox")
    on_disk = script.encode("utf-8-sig")
    # Regression: both of the things that make a UTF-8 batch file decode
    # correctly under cmd.exe are now present.
    assert on_disk.startswith(b"\xef\xbb\xbf")                # UTF-8 BOM
    assert "chcp 65001" in script                             # code-page switch


def test_ascii_only_install_path_still_round_trips_through_any_codepage():
    """Control, kept from before the fix: an ASCII-only path round-trips
    through the OEM code page regardless - which is exactly why this defect
    was invisible to anyone testing (or developing) on an English path."""
    script = _script_for(r"C:\Games\BridgeBox")
    assert script.encode("utf-8").decode("cp866") == script


# --------------------------------------------------------------------------
# 2. download_exe accepts a truncated body when Content-Length is absent
# --------------------------------------------------------------------------


class _TruncatedResp:
    status = 200
    url = "https://objects.githubusercontent.com/asset.zip"
    headers: dict = {}  # <-- no Content-Length

    def __init__(self):
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        pass

    async def iter_chunked(self, n):
        # The "real" asset is many MB; the peer sends 32 bytes and closes.
        yield b"PK\x03\x04 truncated - not the whole archive"


class _TruncatedSession:
    def get(self, url, **kw):
        return _TruncatedResp()


def test_download_exe_still_accepts_a_short_read_with_no_expected_size(tmp_path):
    """Control: with no expected_size at all (the pre-fix call shape),
    there is still nothing to compare against - this half of the fix is
    additive (a new opt-in parameter), not a change to the old default."""
    dest = tmp_path / "asset.zip"
    result = asyncio.run(
        app_update.download_exe(
            _TruncatedSession(),
            "https://objects.githubusercontent.com/asset.zip",
            dest,
        )
    )
    assert result == dest
    assert dest.read_bytes() == b"PK\x03\x04 truncated - not the whole archive"


def test_download_exe_now_rejects_a_short_read_using_the_release_size(tmp_path):
    """Regression: the real caller (_apply_app_update_coro) now passes
    expected_size=release.asset_size - GitHub's own Releases API figure,
    independent of whatever this specific download response's headers say.
    A CDN edge or proxy that drops both Content-Length and the connection
    early can no longer walk a truncated archive past this check."""
    dest = tmp_path / "asset.zip"
    with pytest.raises(app_update.IncompleteDownload):
        asyncio.run(
            app_update.download_exe(
                _TruncatedSession(),
                "https://objects.githubusercontent.com/asset.zip",
                dest,
                expected_size=50_000_000,
                attempts=1,
            )
        )
    assert not dest.exists()  # the truncated attempt is cleaned up, not left behind


# --------------------------------------------------------------------------
# 3. verify_exe_digest does nothing when the digest is missing
# --------------------------------------------------------------------------


def test_verify_exe_digest_is_still_deliberately_a_noop_for_a_missing_digest(tmp_path):
    tampered = tmp_path / "bridgebox-release.zip"
    tampered.write_bytes(b"this is not the file GitHub recorded")

    # Documented, intentional: a release whose API entry carries no `digest`
    # (uploaded before GitHub added the field in mid-2025) still gets no
    # digest check - see verify_exe_digest's own docstring. Not a bug on its
    # own; it only became a real risk paired with finding 2's now-fixed
    # truncation gap.
    assert app_update.verify_exe_digest(tampered, None) is None
