"""Locale resolution and lookups for the backend's own user-facing text
(tray, browser stub pages, the ApplyFailed message) - not the app's UI text,
which lives in frontend/src/data/strings/{ru,en}.json and never touches this."""
from unittest.mock import patch

from bridgebox import i18n


def test_an_explicit_choice_resolves_to_itself():
    assert i18n.resolve_locale("ru") == "ru"
    assert i18n.resolve_locale("en") == "en"


def test_system_falls_back_to_the_os_language():
    with patch.object(i18n, "detect_system_locale", return_value="en"):
        assert i18n.resolve_locale("system") == "en"


def test_a_garbled_preference_resolves_like_system_rather_than_raising():
    """config.yaml can be hand-edited. A typo there should not crash the
    tray icon - it should read as "system", same as an unset value."""
    with patch.object(i18n, "detect_system_locale", return_value="ru"):
        assert i18n.resolve_locale("de") == "ru"


def test_detect_system_locale_only_recognises_russian():
    """Everything that is not Russian falls back to English rather than
    guessing - a wrong guess for a language nobody asked for is worse than
    the safe default."""
    with patch("locale.getlocale", return_value=("Russian_Russia", "1251")):
        assert i18n.detect_system_locale() == "ru"
    with patch("locale.getlocale", return_value=("German_Germany", "1252")):
        assert i18n.detect_system_locale() == "en"
    with patch("locale.getlocale", side_effect=Exception("boom")):
        assert i18n.detect_system_locale() == "en"


def test_t_looks_up_both_shipped_languages():
    assert i18n.t("tray.quit", "ru") == "Выход"
    assert i18n.t("tray.quit", "en") == "Quit"


def test_t_fills_in_placeholders():
    text = i18n.t("tray.tooltip_running", "en", title="BridgeBox")
    assert text == "BridgeBox — bridge is running"


def test_t_falls_back_to_russian_for_an_unshipped_language():
    """A translation nobody wrote yet should read oddly, not take the app
    down - the same principle strings.ts's compile-time check enforces on
    the frontend side, applied at runtime here since Python has no such
    check for a plain dict."""
    assert i18n.t("tray.quit", "de") == i18n.t("tray.quit", "ru")
