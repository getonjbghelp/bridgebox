"""Regression tests for two defects an adversarial audit found in the
BridgeBox self-updater (both now fixed):

1. is_newer() used to raise instead of returning a bool for a version string
   with a long run of digits (CPython's int-from-str 4300-digit cap). Both
   bridgebox.app_update.is_newer and bridgebox.zapret.update.is_newer. The
   version string comes from a GitHub release `tag_name`, which is free-form
   and length-unbounded, and app_update.fetch_latest_release puts it straight
   into AppRelease.version with no sanity check - a hostile/broken release
   tag broke update checks entirely for as long as it stayed "latest". Fixed
   in _numeric_parts (app_update.py) and parts() (zapret/update.py): both now
   catch the ValueError and return () - "unparseable", same as an empty
   match, exactly what the module's own docstring always promised.

2. The apply/swap path (api/app_update.AppUpdateMixin._apply_app_update_coro)
   never called is_newer at all - it downloaded and staged whatever
   fetch_latest_release returned. If GitHub's releases/latest resolved to a
   version <= the installed one (a maintainer re-point, a rollback, or a
   release-pipeline compromise that app_update's own docstring treats as in
   scope), start_app_apply_update staged it and restart_after_app_update
   would have swapped the older bridgebox.exe + _internal/ in - a forced
   downgrade to a known-vulnerable build, via the very mechanism that is
   supposed to deliver critical security fixes. Fixed by adding the same
   is_newer(release.version, app_version()) check the UI banner already
   used, right after fetching the release and before downloading anything.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from bridgebox import app_update
from bridgebox.zapret import update as zapret_update


# --------------------------------------------------------------------------
# Defect 1: is_newer() blows up on a long digit run
# --------------------------------------------------------------------------

HUGE = "1." + "9" * 5000  # 5000-digit component, well past CPython's 4300 cap


def test_app_update_is_newer_no_longer_raises_on_huge_version_component():
    # Regression: the docstring promises "not newer" for anything
    # unparseable; a 5000-digit component used to raise ValueError instead.
    assert app_update.is_newer(HUGE, "0.1.0") is False


def test_app_update_is_newer_no_longer_raises_with_huge_on_installed_side():
    # Regression: same fix covers the frozen build's own version string
    # ever being malformed, not just the remote one.
    assert app_update.is_newer("0.1.0", HUGE) is False


def test_zapret_update_is_newer_no_longer_has_the_same_bug():
    # Regression: zapret/update.py's own is_newer parsed the Flowseal
    # tag_name the same unguarded way - fixed alongside app_update's.
    assert zapret_update.is_newer(HUGE, "1.9.9") is False


def test_numeric_parts_no_longer_raises_at_the_root_cause():
    # Regression: app_update._numeric_parts is where the fix actually lives.
    assert app_update._numeric_parts("v" + "9" * 5000) == ()


@pytest.mark.asyncio
async def test_fetch_latest_release_accepts_an_unbounded_version_then_is_newer_explodes():
    """End to end: a hostile/broken release tag flows through
    fetch_latest_release untouched, and the caller's is_newer() call (which
    in api/app_update.py:47 sits OUTSIDE the try/except) then raises.
    """

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        async def json(self):
            return {
                "tag_name": "v" + "9" * 6000,
                "name": "totally normal release",
                "body": "",
                "html_url": "https://github.com/getonjbghelp/bridgebox/releases/latest",
                "assets": [],
            }

    class _Session:
        def get(self, url, headers=None):
            return _Resp()

    release = await app_update.fetch_latest_release(_Session())
    assert len(release.version) > 4300  # still accepted verbatim, no length guard
    # Regression: this is exactly the call _check_app_update_coro makes at
    # return time - it must report "not newer" instead of raising.
    assert app_update.is_newer(release.version, "0.1.0") is False


# --------------------------------------------------------------------------
# Defect 2: the apply path used to stage a DOWNGRADE without complaint
# --------------------------------------------------------------------------


class _FakeApiSelf:
    """Just enough of desktop.Api for AppUpdateMixin._apply_app_update_coro."""

    def __init__(self, tmp_path: Path):
        self._tmp = tmp_path
        self._app_apply_state: dict = {"phase": "download", "received": 0, "total": 0}
        self._project_root = tmp_path

    def _temp_root(self) -> Path:
        d = self._tmp / "temp"
        d.mkdir(parents=True, exist_ok=True)
        return d


@pytest.mark.asyncio
async def test_apply_app_update_now_refuses_to_stage_a_downgrade(
    tmp_path, monkeypatch
):
    from bridgebox.api import app_update as api_app_update

    # Pretend we are a frozen onedir install.
    exe_path = tmp_path / "bridgebox.exe"
    exe_path.write_bytes(b"MZ current")
    internal_dir = tmp_path / "_internal"
    internal_dir.mkdir()
    (internal_dir / "python.dll").write_bytes(b"current runtime")

    monkeypatch.setattr(app_update, "running_exe_path", lambda: exe_path)
    monkeypatch.setattr(app_update, "running_internal_dir", lambda: internal_dir)

    # The installed version is 9.9.9; GitHub "latest" comes back as 0.0.1.
    monkeypatch.setattr(
        "bridgebox.api.app_update.app_version", lambda: "9.9.9", raising=True
    )

    older = app_update.AppRelease(
        version="0.0.1",
        name="rollback",
        notes="",
        html_url="https://github.com/getonjbghelp/bridgebox/releases/latest",
        critical=False,
        asset_url="https://github.com/getonjbghelp/bridgebox/releases/download/v0.0.1/BridgeBox_Portable.zip",
        asset_size=10,
        asset_digest=None,
        asset_is_archive=True,
    )

    async def _fake_fetch(session, **kw):
        return older

    async def _fake_download(session, url, dest, **kw):
        Path(dest).write_bytes(b"PK\x03\x04 fake archive")
        return Path(dest)

    def _fake_extract(archive, exe_stage, internal_stage, **kw):
        Path(exe_stage).write_bytes(b"MZ OLD 0.0.1")
        Path(internal_stage).mkdir(parents=True, exist_ok=True)
        (Path(internal_stage) / "python.dll").write_bytes(b"OLD runtime 0.0.1")
        return Path(exe_stage)

    monkeypatch.setattr(app_update, "fetch_latest_release", _fake_fetch)
    monkeypatch.setattr(app_update, "download_exe", _fake_download)
    monkeypatch.setattr(app_update, "verify_exe_digest", lambda p, d: None)
    monkeypatch.setattr(app_update, "extract_release_from_archive", _fake_extract)

    me = _FakeApiSelf(tmp_path)
    result = await api_app_update.AppUpdateMixin._apply_app_update_coro(me)

    # Regression: a self-update flow that exists to push CRITICAL security
    # fixes must refuse a release that is not actually newer, rather than
    # staging it - nothing gets downloaded past the fake fetch, and nothing
    # is left on disk for restart_after_app_update to swap in.
    assert result["ok"] is False
    assert result["version"] is None
    assert not app_update.stage_path_for(exe_path).exists()
    assert not app_update.stage_path_for(internal_dir).exists()
