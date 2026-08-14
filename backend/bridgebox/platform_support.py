"""Refusing to start on a Windows that cannot run this app.

Windows 7, 8 and 8.1 are not a "mostly works, some rough edges" case here.
The UI is WebView2, and Microsoft ended WebView2 support for all three of
them - the runtime will not install, so the window would come up empty with
no explanation. Failing loudly at startup is the honest outcome; limping
into a blank window and letting the user file a bug about it is not.

The version is read through RtlGetVersion rather than GetVersionEx (which is
what sys.getwindowsversion() uses). GetVersionEx is subject to compatibility
shimming: an app running in "Windows 8 compatibility mode", or one without
the right manifest, is told whatever the shim decided. That is precisely the
machine this gate exists to catch, so it has to ask the kernel directly.
"""
from __future__ import annotations

import ctypes
import logging
import sys
import tempfile
import webbrowser
from ctypes import wintypes
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

# Windows 10 is the floor. Everything older reports major 6 (8.1 = 6.3,
# 8 = 6.2, 7 = 6.1) or lower, so one comparison covers all of them.
MINIMUM_MAJOR = 10

# The build the DWM caption-colour attributes arrive in - see window_chrome.
WINDOWS_11_BUILD = 22000


class WindowsVersion(NamedTuple):
    major: int
    minor: int
    build: int

    @property
    def is_windows_11(self) -> bool:
        return self.major >= 10 and self.build >= WINDOWS_11_BUILD

    def describe(self) -> str:
        if self.is_windows_11:
            name = "Windows 11"
        elif self.major == 10:
            name = "Windows 10"
        elif (self.major, self.minor) == (6, 3):
            name = "Windows 8.1"
        elif (self.major, self.minor) == (6, 2):
            name = "Windows 8"
        elif (self.major, self.minor) == (6, 1):
            name = "Windows 7"
        else:
            name = "Windows"
        return f"{name} ({self.major}.{self.minor}, сборка {self.build})"


class _OSVERSIONINFOEXW(ctypes.Structure):
    _fields_ = [
        ("dwOSVersionInfoSize", wintypes.DWORD),
        ("dwMajorVersion", wintypes.DWORD),
        ("dwMinorVersion", wintypes.DWORD),
        ("dwBuildNumber", wintypes.DWORD),
        ("dwPlatformId", wintypes.DWORD),
        ("szCSDVersion", wintypes.WCHAR * 128),
        ("wServicePackMajor", wintypes.WORD),
        ("wServicePackMinor", wintypes.WORD),
        ("wSuiteMask", wintypes.WORD),
        ("wProductType", ctypes.c_byte),
        ("wReserved", ctypes.c_byte),
    ]


def current_windows_version() -> WindowsVersion | None:
    """The real Windows version, or None when not running on Windows.

    None is not "unsupported" - it is "this gate does not apply", which is
    what keeps the test suite runnable off Windows."""
    try:
        rtl_get_version = ctypes.windll.ntdll.RtlGetVersion  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None

    info = _OSVERSIONINFOEXW()
    info.dwOSVersionInfoSize = ctypes.sizeof(info)
    if rtl_get_version(ctypes.byref(info)) != 0:  # STATUS_SUCCESS
        return None
    return WindowsVersion(info.dwMajorVersion, info.dwMinorVersion, info.dwBuildNumber)


def is_supported(version: WindowsVersion | None) -> bool:
    """Whether BridgeBox can run here. None (not Windows) is not blocked."""
    return version is None or version.major >= MINIMUM_MAJOR


UNSUPPORTED_TITLE = "BridgeBox: эта версия Windows не поддерживается"


def unsupported_html(version: WindowsVersion | None) -> str:
    """A self-contained page explaining the refusal.

    Self-contained because it has to render on a machine where the app's own
    UI provably cannot: WebView2 is exactly what is missing there, so the
    page goes to the default browser instead of a pywebview window. No
    external CSS or fonts for the same reason - this may be an old machine
    with no network."""
    detected = version.describe() if version else "не удалось определить"
    return f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>{UNSUPPORTED_TITLE}</title>
<style>
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center;
    justify-content: center; background: #0a0f1e; color: #e8eef8;
    font: 15px/1.6 "Segoe UI", system-ui, sans-serif;
  }}
  main {{ max-width: 560px; padding: 40px; }}
  h1 {{ font-size: 22px; margin: 0 0 20px; }}
  p {{ color: #a3adbd; }}
  .detected {{
    margin: 24px 0; padding: 12px 16px; background: #111a2e;
    border: 1px solid #223049; border-radius: 8px;
    font-family: Consolas, monospace; font-size: 13px; color: #e8eef8;
  }}
  strong {{ color: #e8eef8; }}
</style>
</head>
<body>
<main>
  <h1>BridgeBox не запускается на этой версии Windows</h1>
  <p>
    Для работы нужна <strong>Windows 10 или новее</strong>. Интерфейс
    BridgeBox построен на компоненте Microsoft Edge WebView2, а Microsoft
    прекратила его поддержку для Windows 7, 8 и 8.1 — установить его на этих
    системах уже нельзя, и окно программы осталось бы пустым.
  </p>
  <div class="detected">Обнаружено: {detected}</div>
  <p>
    Обновитесь до Windows 10 или Windows 11. Других способов запустить
    BridgeBox на этой системе нет.
  </p>
</main>
</body>
</html>
"""


def _message_box(text: str) -> bool:
    """Last-resort notice. Always available - user32 is on every Windows
    back to the ones this gate rejects, which is the whole point of using it
    as the fallback for a browser that might not open."""
    try:
        # MB_OK | MB_ICONERROR | MB_SETFOREGROUND
        ctypes.windll.user32.MessageBoxW(None, text, UNSUPPORTED_TITLE, 0x10 | 0x10000)
        return True
    except (AttributeError, OSError):
        return False


def show_unsupported_notice(version: WindowsVersion | None, *, open_page=None) -> None:
    """Tell the user why nothing is going to happen.

    Tries the HTML page first because it can actually explain, and falls back
    to a MessageBox if the browser cannot be opened - on a machine this old,
    "the app silently did nothing" is the one outcome worth ruling out."""
    detected = version.describe() if version else "не удалось определить"
    plain = (
        "BridgeBox требует Windows 10 или новее.\n\n"
        f"Обнаружено: {detected}\n\n"
        "Интерфейс использует Microsoft Edge WebView2, поддержка которого "
        "для Windows 7, 8 и 8.1 прекращена. Обновите систему."
    )

    opener = open_page if open_page is not None else webbrowser.open
    try:
        page = Path(tempfile.gettempdir()) / "bridgebox-unsupported-windows.html"
        page.write_text(unsupported_html(version), encoding="utf-8")
        if opener(page.as_uri()):
            return
    except Exception as exc:  # noqa: BLE001 - any failure falls through to the box
        logger.debug("could not open the unsupported-Windows page: %s", exc)

    if not _message_box(plain) and sys.stderr is not None:
        # Neither a browser nor user32 - say it on the console and let the
        # SystemExit message the caller prints carry the rest. Guarded: the
        # frozen build's windowed subsystem has no console at all, and this
        # runs before logging exists to catch a write to a None stream.
        print(plain, file=sys.stderr)
