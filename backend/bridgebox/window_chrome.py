"""Painting the native window title bar to match the app's theme.

The title bar is drawn by Windows, not by the webview, so it ignored every
token in tokens.css and sat there as a light strip above a dark interface.

Deliberately NOT solved with `frameless=True` + an HTML title bar. That is
the other common answer and it is a bad trade here: it hands back snap
layouts, Aero Shake, double-click-to-maximise, the Alt+Space system menu,
the drag-to-monitor-edge behaviours, and the OS-correct hit targets on the
caption buttons - all of which then have to be reimplemented, badly, in
JavaScript. DWM lets the system keep drawing its own title bar and only
changes what colour it uses, so none of that is given up.

Windows 11 (build 22000+) is what makes this possible: DWMWA_CAPTION_COLOR
and friends simply do not exist before it. Verified against this project's
target - build 26200 returns S_OK for all four attributes used here. On
anything older the caption-colour calls fail harmlessly and only the
dark-mode flag applies, which is still better than nothing and is why every
call here is independent rather than one all-or-nothing block.
"""
from __future__ import annotations

import ctypes
import logging
from typing import NamedTuple

from .platform_support import WINDOWS_11_BUILD, current_windows_version

logger = logging.getLogger(__name__)

# DWMWA_USE_IMMERSIVE_DARK_MODE was undocumented and numbered 19 until
# Windows 10 2004 (build 19041) renumbered it to 20. Picking by build rather
# than trying both blind matters: on the builds where 19 is the live one, 20
# is simply a different (or unassigned) attribute, and setting it can return
# S_OK while doing nothing at all - a silent no-op that looks like success.
DARK_MODE_RENUMBERED_BUILD = 19041

# https://learn.microsoft.com/windows/win32/api/dwmapi/ne-dwmapi-dwmwindowattribute
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
# Windows 10 builds 18985-19041 used a different ordinal for the same flag.
DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY = 19
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36


class TitlebarTheme(NamedTuple):
    """What the caption should look like for one app theme.

    The values are the same ones tokens.css resolves to - `--color-surface`
    for the bar, `--color-text-primary` for its text, `--color-border` for
    the window outline. Surface rather than `--color-bg` on purpose: the
    title bar is chrome, and the sidebar directly beneath its left end is
    the same colour, so the two read as one continuous frame around the
    content canvas.
    """

    caption: str
    text: str
    border: str
    dark_mode: bool


TITLEBAR_THEMES: dict[str, TitlebarTheme] = {
    "light": TitlebarTheme(
        caption="#ffffff",  # --color-surface  (--slate-0)
        text="#0f172a",  # --color-text-primary (--slate-900)
        border="#e2e8f0",  # --color-border (--slate-200)
        dark_mode=False,
    ),
    "dark": TitlebarTheme(
        caption="#111a2e",  # --color-surface
        text="#e8eef8",  # --color-text-primary
        border="#223049",  # --color-border
        dark_mode=True,
    ),
}


def colorref(hex_color: str) -> int:
    """#RRGGBB -> Win32 COLORREF.

    COLORREF is 0x00BBGGRR - byte-reversed from the way the same colour is
    written in CSS. Converted here rather than stored pre-swapped so the
    constants above stay copy-pasteable against tokens.css; a hand-swapped
    literal is unreviewable and silently wrong if it is off."""
    value = hex_color.lstrip("#")
    if len(value) != 6:
        raise ValueError(f"expected #RRGGBB, got {hex_color!r}")
    red, green, blue = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    return (blue << 16) | (green << 8) | red


def _load_dwmapi():
    """The DWM API, or None off Windows / where it is unavailable."""
    try:
        return ctypes.windll.dwmapi  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None


def window_handle(window) -> int | None:
    """The HWND behind a pywebview window, or None if it has none yet.

    pywebview's WinForms backend assigns the Form itself to `window.native`
    once the window exists, so its .Handle is the HWND. None before that -
    which is why callers wait for the `shown` event rather than applying
    this straight after create_window()."""
    native = getattr(window, "native", None)
    handle = getattr(native, "Handle", None)
    if handle is None:
        return None
    try:
        return int(handle)
    except (TypeError, ValueError):
        # pythonnet hands back a System.IntPtr; older versions need this.
        to_int64 = getattr(handle, "ToInt64", None)
        try:
            return int(to_int64()) if to_int64 is not None else None
        except Exception:
            return None


def _dark_mode_attribute(version) -> int:
    """Which ordinal carries the dark-mode flag on this Windows."""
    if version is None or version.build >= DARK_MODE_RENUMBERED_BUILD:
        return DWMWA_USE_IMMERSIVE_DARK_MODE
    return DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY


def _repaint_frame(hwnd: int) -> None:
    """Force the non-client area to redraw.

    Windows 10 accepts the dark-mode attribute and then leaves the old
    caption on screen until something else invalidates the frame - so the
    title bar only flips colour once the window is moved or resized, which
    reads as the setting not working. SWP_FRAMECHANGED asks for that
    invalidation without moving, resizing or restacking anything. Harmless
    on Windows 11, where the repaint already happens on its own."""
    SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x0001, 0x0002, 0x0004
    SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x0010, 0x0020
    try:
        ctypes.windll.user32.SetWindowPos(  # type: ignore[attr-defined]
            ctypes.c_void_p(hwnd),
            None,
            0,
            0,
            0,
            0,
            SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )
    except (AttributeError, OSError) as exc:
        logger.debug("could not force a frame repaint: %s", exc)


def _set_attribute(dwmapi, hwnd: int, attribute: int, value: int) -> bool:
    data = ctypes.c_int(value)
    try:
        result = dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(hwnd),
            ctypes.c_uint(attribute),
            ctypes.byref(data),
            ctypes.c_uint(ctypes.sizeof(data)),
        )
    except (AttributeError, OSError) as exc:
        logger.debug("DwmSetWindowAttribute(%d) unavailable: %s", attribute, exc)
        return False
    return result == 0


# How far the title bar could be themed on this machine.
THEMED_FULL = "full"  # Windows 11: exact brand colours on the caption
THEMED_DARK_MODE = "dark-mode"  # Windows 10: light/dark caption, its own colours
THEMED_NONE = "none"


def apply_titlebar_theme(window, theme: str, *, dwmapi=None, version=None) -> str:
    """Repaint `window`'s native title bar for the given app theme.

    Best-effort and never raises: a title bar that stays the wrong colour is
    a blemish, and it must not be able to take down the app that owns it -
    the same rule install_ca_windows and _harden_key_permissions follow.

    Returns how far it got, because "worked" is genuinely three-valued here:

      THEMED_FULL       Windows 11 (22000+). The caption, its text and the
                        window border take the app's own colours.
      THEMED_DARK_MODE  Windows 10. The colour attributes do not exist, but
                        the caption still follows the app's light/dark
                        choice instead of the system's - which is the whole
                        of what Windows 10 offers and is worth having.
      THEMED_NONE       Nothing applied: no window yet, unknown theme, or no
                        dwmapi at all.
    """
    palette = TITLEBAR_THEMES.get(theme)
    if palette is None:
        logger.debug("no title bar palette for theme %r", theme)
        return THEMED_NONE

    hwnd = window_handle(window)
    if hwnd is None:
        logger.debug("window has no native handle yet - title bar left alone")
        return THEMED_NONE

    api = dwmapi if dwmapi is not None else _load_dwmapi()
    if api is None:
        return THEMED_NONE

    if version is None:
        version = current_windows_version()

    # The caption buttons' glyphs (minimise/maximise/close) follow this, not
    # the caption colour - without it they stay dark-on-dark and the close X
    # is nearly invisible on the dark theme. On Windows 10 this is also the
    # only thing that works, so it is what makes the title bar track the
    # app's theme there at all.
    dark = 1 if palette.dark_mode else 0
    attribute = _dark_mode_attribute(version)
    dark_ok = _set_attribute(api, hwnd, attribute, dark)
    if not dark_ok:
        # A build right on the renumbering boundary, or a version we could
        # not read - try the other ordinal rather than give up on the one
        # thing Windows 10 can do.
        other = (
            DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY
            if attribute == DWMWA_USE_IMMERSIVE_DARK_MODE
            else DWMWA_USE_IMMERSIVE_DARK_MODE
        )
        dark_ok = _set_attribute(api, hwnd, other, dark)

    caption_ok = _set_attribute(api, hwnd, DWMWA_CAPTION_COLOR, colorref(palette.caption))
    _set_attribute(api, hwnd, DWMWA_TEXT_COLOR, colorref(palette.text))
    _set_attribute(api, hwnd, DWMWA_BORDER_COLOR, colorref(palette.border))

    # Windows 10 will not redraw the caption on its own - see _repaint_frame.
    if dark_ok or caption_ok:
        _repaint_frame(hwnd)

    if caption_ok:
        logger.debug("title bar themed: %s (%s)", theme, palette.caption)
        return THEMED_FULL
    if dark_ok:
        logger.debug("title bar follows the %s theme (Windows 10: no caption colours)", theme)
        return THEMED_DARK_MODE
    logger.debug("title bar could not be themed on this Windows")
    return THEMED_NONE
