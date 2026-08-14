from bridgebox.platform_support import (
    MINIMUM_MAJOR,
    WINDOWS_11_BUILD,
    WindowsVersion,
    is_supported,
    show_unsupported_notice,
    unsupported_html,
)


# ---- WindowsVersion ---------------------------------------------------


def test_windows_7_is_recognised_and_described():
    v = WindowsVersion(6, 1, 7601)
    assert v.is_windows_11 is False
    assert "Windows 7" in v.describe()


def test_windows_8_and_8_1_are_told_apart():
    """8 and 8.1 share major version 6 and differ only in minor - a version
    string that collapsed them would misreport which one was actually
    detected."""
    assert "Windows 8.1" in WindowsVersion(6, 3, 9600).describe()
    assert "Windows 8.1" not in WindowsVersion(6, 2, 9200).describe()
    assert "Windows 8" in WindowsVersion(6, 2, 9200).describe()


def test_windows_10_and_11_are_told_apart_by_build_not_major_version():
    """Windows 11 reports major=10 - same as Windows 10 - and is only
    distinguishable by build number crossing WINDOWS_11_BUILD. Getting this
    wrong would either call a real Windows 11 machine "Windows 10" in the
    unsupported-notice page (it never reaches that page, but the title bar
    code depends on the same distinction) or vice versa."""
    ten = WindowsVersion(10, 0, WINDOWS_11_BUILD - 1)
    eleven = WindowsVersion(10, 0, WINDOWS_11_BUILD)
    assert ten.is_windows_11 is False
    assert "Windows 10" in ten.describe()
    assert eleven.is_windows_11 is True
    assert "Windows 11" in eleven.describe()


def test_an_unrecognised_major_version_still_describes_something():
    """Must never raise on a version this app has never seen - a garbled or
    future report should degrade to a generic label, not crash the gate that
    exists to fail safely."""
    assert WindowsVersion(5, 1, 2600).describe()  # Windows XP; still returns a string


# ---- is_supported -------------------------------------------------------


def test_windows_7_8_and_8_1_are_unsupported():
    assert is_supported(WindowsVersion(6, 1, 7601)) is False  # 7
    assert is_supported(WindowsVersion(6, 2, 9200)) is False  # 8
    assert is_supported(WindowsVersion(6, 3, 9600)) is False  # 8.1


def test_windows_10_and_11_are_supported():
    assert is_supported(WindowsVersion(10, 0, 19045)) is True
    assert is_supported(WindowsVersion(10, 0, 22631)) is True


def test_minimum_major_is_ten():
    """Pinned so a future edit that loosens or tightens the floor is a
    visible, deliberate diff here, not a side effect of something else."""
    assert MINIMUM_MAJOR == 10


def test_none_version_is_not_blocked():
    """None means "could not read the Windows version" (including "this is
    not Windows at all", which is how the test suite itself runs) - it must
    not be treated as an old, unsupported Windows."""
    assert is_supported(None) is True


# ---- the notice page ------------------------------------------------------


def test_unsupported_html_names_the_detected_version():
    html = unsupported_html(WindowsVersion(6, 1, 7601))
    assert "Windows 7" in html
    assert "7601" in html


def test_unsupported_html_handles_an_undetectable_version():
    html = unsupported_html(None)
    assert "<html" in html
    assert "не удалось определить" in html


def test_unsupported_html_has_no_external_references():
    """This page's entire reason to exist is rendering on a machine that may
    have no network and, more to the point, cannot run BridgeBox's own UI -
    a stylesheet or font fetched from anywhere would just fail quietly and
    leave the page unstyled or blank."""
    html = unsupported_html(WindowsVersion(6, 1, 7601))
    assert "http://" not in html
    assert "https://" not in html
    assert '<link' not in html


# ---- show_unsupported_notice ----------------------------------------------


def test_show_unsupported_notice_opens_a_page_when_it_can(tmp_path, monkeypatch):
    import bridgebox.platform_support as platform_support

    monkeypatch.setattr(platform_support.tempfile, "gettempdir", lambda: str(tmp_path))
    opened = []

    show_unsupported_notice(WindowsVersion(6, 1, 7601), open_page=lambda uri: opened.append(uri) or True)

    assert len(opened) == 1
    assert opened[0].startswith("file:")
    written = tmp_path / "bridgebox-unsupported-windows.html"
    assert written.exists()
    assert "Windows 7" in written.read_text(encoding="utf-8")


def test_show_unsupported_notice_falls_back_to_a_message_box(tmp_path, monkeypatch):
    """The browser refusing to open (opener returns False, e.g. no default
    browser registered) must not mean the user sees nothing at all."""
    import bridgebox.platform_support as platform_support

    monkeypatch.setattr(platform_support.tempfile, "gettempdir", lambda: str(tmp_path))
    box_calls = []
    monkeypatch.setattr(platform_support, "_message_box", lambda text: box_calls.append(text) or True)

    show_unsupported_notice(WindowsVersion(6, 1, 7601), open_page=lambda uri: False)

    assert len(box_calls) == 1
    assert "Windows 10" in box_calls[0]


def test_show_unsupported_notice_falls_back_when_writing_the_page_fails(monkeypatch):
    """No writable temp dir on a machine this constrained is not impossible -
    the notice still has to reach the user somehow."""
    import bridgebox.platform_support as platform_support

    def exploding_open(uri):
        raise AssertionError("must not be called - the page was never written")

    box_calls = []
    monkeypatch.setattr(platform_support, "_message_box", lambda text: box_calls.append(text) or True)
    monkeypatch.setattr(
        platform_support.tempfile, "gettempdir", lambda: (_ for _ in ()).throw(OSError("no temp dir"))
    )

    show_unsupported_notice(WindowsVersion(6, 1, 7601), open_page=exploding_open)

    assert len(box_calls) == 1


def test_show_unsupported_notice_never_raises_even_if_everything_fails(monkeypatch, capsys):
    """The last resort (stderr) must still run - this function exists
    specifically for a machine already in a degraded state, so every layer
    beneath it is allowed to fail."""
    import bridgebox.platform_support as platform_support

    monkeypatch.setattr(
        platform_support.tempfile, "gettempdir", lambda: (_ for _ in ()).throw(OSError("no temp"))
    )
    monkeypatch.setattr(platform_support, "_message_box", lambda text: False)

    show_unsupported_notice(WindowsVersion(6, 1, 7601), open_page=lambda uri: False)

    captured = capsys.readouterr()
    assert "Windows 10" in captured.err
