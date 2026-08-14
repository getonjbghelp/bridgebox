from bridgebox import window_chrome
from bridgebox.platform_support import WindowsVersion
from bridgebox.window_chrome import (
    DWMWA_BORDER_COLOR,
    DWMWA_CAPTION_COLOR,
    DWMWA_TEXT_COLOR,
    DWMWA_USE_IMMERSIVE_DARK_MODE,
    DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY,
    THEMED_DARK_MODE,
    THEMED_FULL,
    THEMED_NONE,
    TITLEBAR_THEMES,
    apply_titlebar_theme,
    colorref,
    window_handle,
)

WIN11 = WindowsVersion(10, 0, 22631)
WIN10_MODERN = WindowsVersion(10, 0, 19045)  # >= the dark-mode renumbering
WIN10_OLD = WindowsVersion(10, 0, 17763)  # < the dark-mode renumbering


class FakeDwmapi:
    """Records the attributes that would have been handed to Windows.

    The real call is verified separately against a live HWND; what these
    tests pin down is the part that is easy to get silently wrong - which
    attribute gets which value, and that a byte-swapped colour is what
    actually goes out."""

    def __init__(self, *, fail: set[int] | None = None):
        self.calls: list[tuple[int, int]] = []
        self._fail = fail or set()

    def DwmSetWindowAttribute(self, hwnd, attribute, value, size):  # noqa: N802 - Win32 name
        import ctypes

        attr = attribute.value if hasattr(attribute, "value") else int(attribute)
        data = ctypes.cast(value, ctypes.POINTER(ctypes.c_int)).contents.value
        self.calls.append((attr, data))
        return 1 if attr in self._fail else 0

    def value_for(self, attribute: int) -> int | None:
        for attr, value in self.calls:
            if attr == attribute:
                return value
        return None


class FakeWindow:
    def __init__(self, handle=4242):
        self.native = type("Native", (), {"Handle": handle})()


# ---- colour conversion ----------------------------------------------------


def test_colorref_reverses_the_byte_order_css_uses():
    """COLORREF is 0x00BBGGRR, CSS is #RRGGBB. Getting this backwards is
    invisible in review and produces a plausible-but-wrong colour, so it is
    pinned with channels that cannot be confused for each other."""
    assert colorref("#ff0000") == 0x0000FF  # pure red lands in the low byte
    assert colorref("#00ff00") == 0x00FF00
    assert colorref("#0000ff") == 0xFF0000  # pure blue lands in the high byte


def test_colorref_accepts_a_bare_hex_string():
    assert colorref("111a2e") == colorref("#111a2e")


def test_colorref_rejects_a_malformed_colour():
    import pytest

    with pytest.raises(ValueError):
        colorref("#fff")


def test_both_themes_have_a_palette_matching_the_design_tokens():
    """These are copied from tokens.css by hand, so a drift here is silent.
    The values are asserted literally: if a token changes, this test is the
    reminder that the title bar has to change with it."""
    assert TITLEBAR_THEMES["dark"].caption == "#111a2e"  # --color-surface
    assert TITLEBAR_THEMES["dark"].text == "#e8eef8"  # --color-text-primary
    assert TITLEBAR_THEMES["light"].caption == "#ffffff"
    assert TITLEBAR_THEMES["light"].text == "#0f172a"
    assert TITLEBAR_THEMES["dark"].dark_mode is True
    assert TITLEBAR_THEMES["light"].dark_mode is False


# ---- handle resolution ----------------------------------------------------


def test_window_handle_reads_the_native_forms_handle():
    assert window_handle(FakeWindow(handle=1234)) == 1234


def test_window_handle_is_none_before_the_window_exists():
    """pywebview leaves window.native as None until the window is created -
    applying the theme any earlier is a no-op, not an error."""

    class NotShownYet:
        native = None

    assert window_handle(NotShownYet()) is None
    assert window_handle(object()) is None


def test_window_handle_falls_back_to_toint64_for_a_system_intptr():
    """pythonnet hands back a System.IntPtr; older versions do not support
    int() on it directly."""

    class IntPtrLike:
        def __int__(self):
            raise TypeError("not directly convertible")

        def ToInt64(self):  # noqa: N802 - .NET name
            return 99


    class Window:
        native = type("Native", (), {"Handle": IntPtrLike()})()

    assert window_handle(Window()) == 99


# ---- applying the theme ---------------------------------------------------


def test_dark_theme_sets_every_attribute_with_swapped_colours_on_windows_11():
    api = FakeDwmapi()

    result = apply_titlebar_theme(FakeWindow(), "dark", dwmapi=api, version=WIN11)

    assert result == THEMED_FULL
    assert api.value_for(DWMWA_USE_IMMERSIVE_DARK_MODE) == 1
    assert api.value_for(DWMWA_CAPTION_COLOR) == colorref("#111a2e")
    assert api.value_for(DWMWA_TEXT_COLOR) == colorref("#e8eef8")
    assert api.value_for(DWMWA_BORDER_COLOR) == colorref("#223049")


def test_light_theme_turns_the_dark_mode_flag_back_off():
    """Without this the caption buttons keep their light glyphs and the
    close X all but disappears against a white title bar."""
    api = FakeDwmapi()

    apply_titlebar_theme(FakeWindow(), "light", dwmapi=api, version=WIN11)

    assert api.value_for(DWMWA_USE_IMMERSIVE_DARK_MODE) == 0
    assert api.value_for(DWMWA_CAPTION_COLOR) == colorref("#ffffff")


def test_an_unknown_theme_changes_nothing():
    api = FakeDwmapi()

    assert apply_titlebar_theme(FakeWindow(), "solarized", dwmapi=api, version=WIN11) == THEMED_NONE
    assert api.calls == []


def test_no_window_handle_is_reported_rather_than_raising():
    class NotShownYet:
        native = None

    api = FakeDwmapi()
    assert apply_titlebar_theme(NotShownYet(), "dark", dwmapi=api, version=WIN11) == THEMED_NONE
    assert api.calls == []


def test_a_broken_dwmapi_never_takes_the_app_down():
    """Best-effort by design: a title bar that stays the wrong colour is a
    blemish, not a reason to fail the process that owns the window."""

    class Exploding:
        def DwmSetWindowAttribute(self, *args):  # noqa: N802 - Win32 name
            raise OSError("dwmapi.dll is having a day")

    result = apply_titlebar_theme(FakeWindow(), "dark", dwmapi=Exploding(), version=WIN11)
    assert result == THEMED_NONE


# ---- Windows 10 -------------------------------------------------------


def test_windows_10_gets_dark_mode_only_not_caption_colours():
    """DWMWA_CAPTION_COLOR does not exist before Windows 11 22000 - the call
    still returns S_OK on some builds while doing nothing, which is exactly
    the silent-no-op this fake has to be trusted not to paper over. Failing
    the colour attributes here is what makes the distinction real."""
    api = FakeDwmapi(fail={DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR, DWMWA_BORDER_COLOR})

    result = apply_titlebar_theme(FakeWindow(), "dark", dwmapi=api, version=WIN10_MODERN)

    assert result == THEMED_DARK_MODE
    assert api.value_for(DWMWA_USE_IMMERSIVE_DARK_MODE) == 1


def test_windows_10_before_the_renumbering_uses_the_legacy_ordinal_directly():
    """Builds 18985-19041 only ever had the flag at attribute 19 - it should
    be asked for by that number first, not discovered by 20 failing."""
    api = FakeDwmapi(fail={DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR, DWMWA_BORDER_COLOR})

    apply_titlebar_theme(FakeWindow(), "dark", dwmapi=api, version=WIN10_OLD)

    assert api.value_for(DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY) == 1
    assert api.value_for(DWMWA_USE_IMMERSIVE_DARK_MODE) is None  # never tried first


def test_the_other_ordinal_is_tried_if_the_expected_one_fails():
    """A build right on the boundary, or a version that could not be read -
    the flag must not simply give up because its first guess was wrong."""
    api = FakeDwmapi(
        fail={DWMWA_USE_IMMERSIVE_DARK_MODE, DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR, DWMWA_BORDER_COLOR}
    )

    result = apply_titlebar_theme(FakeWindow(), "dark", dwmapi=api, version=WIN11)

    assert result == THEMED_DARK_MODE
    assert api.value_for(DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY) == 1


def test_windows_10_gets_a_repaint_nudge_but_windows_11_repaint_is_harmless():
    """Windows 10 does not redraw the caption on its own after the attribute
    changes - see _repaint_frame. Patched rather than left to hit the real
    user32 call, so the test asserts the nudge happened instead of merely
    tolerating whatever SetWindowPos does to a fake handle."""
    calls = []

    class FakeWindow2(FakeWindow):
        pass

    original = window_chrome._repaint_frame
    window_chrome._repaint_frame = lambda hwnd: calls.append(hwnd)
    try:
        api = FakeDwmapi(fail={DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR, DWMWA_BORDER_COLOR})
        apply_titlebar_theme(FakeWindow2(handle=777), "dark", dwmapi=api, version=WIN10_MODERN)
    finally:
        window_chrome._repaint_frame = original

    assert calls == [777]


def test_no_repaint_nudge_when_nothing_at_all_could_be_applied():
    calls = []
    original = window_chrome._repaint_frame
    window_chrome._repaint_frame = lambda hwnd: calls.append(hwnd)
    try:

        class Exploding:
            def DwmSetWindowAttribute(self, *args):  # noqa: N802 - Win32 name
                return 1  # E_FAIL for every attribute

        apply_titlebar_theme(FakeWindow(), "dark", dwmapi=Exploding(), version=WIN11)
    finally:
        window_chrome._repaint_frame = original

    assert calls == []


def test_dark_mode_attribute_picks_the_ordinal_by_build():
    assert window_chrome._dark_mode_attribute(WIN10_OLD) == DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY
    assert window_chrome._dark_mode_attribute(WIN10_MODERN) == DWMWA_USE_IMMERSIVE_DARK_MODE
    assert window_chrome._dark_mode_attribute(WIN11) == DWMWA_USE_IMMERSIVE_DARK_MODE
    # Unknown version (current_windows_version() found nothing): assume modern
    # rather than guess the far more restrictive legacy ordinal.
    assert window_chrome._dark_mode_attribute(None) == DWMWA_USE_IMMERSIVE_DARK_MODE


def test_apply_titlebar_theme_reads_the_real_version_when_none_is_given():
    """The version= parameter exists for tests; production calls it with
    nothing and must still pick the right ordinal for the machine it is
    actually running on."""
    api = FakeDwmapi(fail={DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR, DWMWA_BORDER_COLOR})
    seen = {}

    def fake_current_version():
        seen["called"] = True
        return WIN10_OLD

    original = window_chrome.current_windows_version
    window_chrome.current_windows_version = fake_current_version
    try:
        apply_titlebar_theme(FakeWindow(), "dark", dwmapi=api)
    finally:
        window_chrome.current_windows_version = original

    assert seen.get("called") is True
    assert api.value_for(DWMWA_USE_IMMERSIVE_DARK_MODE_LEGACY) == 1
