"""validate_people() - the one piece of tools/build_content.py's people.json
support worth a regression test. Everything else in that script is HTTP
plumbing or generated HTML, exercised by running the editor, not a unit
test standing in for one."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import build_content  # noqa: E402
from build_content import validate_people  # noqa: E402


def _base(**overrides: object) -> dict:
    data = {
        "donators": [
            {
                "id": "d1",
                "name": "Alice",
                "date": "2026-01-01",
                "platform": "Donatty",
            }
        ],
        "bughunters": [
            {
                "id": "b1",
                "name": "Bob",
                "bugTitle": {"ru": "Заголовок", "en": "Title"},
                "bugDescription": {"ru": "Описание", "en": "Description"},
            }
        ],
        "testers": [
            {
                "id": "t1",
                "name": "Carol",
                "tested": {"ru": "Мастер", "en": "Wizard"},
                "environment": "Windows 11",
                "contribution": {"ru": "Вклад", "en": "Contribution"},
            }
        ],
        "other": [
            {
                "id": "o1",
                "name": "Dave",
                "reason": {"ru": "Причина", "en": "Reason"},
            }
        ],
    }
    data.update(overrides)
    return data


def test_a_minimal_valid_entry_per_category_round_trips():
    result = validate_people(_base())
    assert result["donators"][0]["name"] == "Alice"
    assert result["bughunters"][0]["bugTitle"] == {"ru": "Заголовок", "en": "Title"}
    assert result["testers"][0]["environment"] == "Windows 11"
    assert result["other"][0]["reason"] == {"ru": "Причина", "en": "Reason"}


def test_other_requires_a_reason():
    data = _base()
    del data["other"][0]["reason"]
    with pytest.raises(ValueError, match="reason"):
        validate_people(data)


def test_a_missing_required_field_is_rejected():
    data = _base()
    del data["donators"][0]["platform"]
    with pytest.raises(ValueError, match="platform"):
        validate_people(data)


def test_a_duplicate_id_within_a_category_is_rejected():
    data = _base()
    data["donators"].append(dict(data["donators"][0]))
    with pytest.raises(ValueError, match="повторяется"):
        validate_people(data)


def test_the_same_id_in_two_different_categories_is_fine():
    data = _base()
    data["bughunters"][0]["id"] = "d1"  # same id as the donator, different category
    validate_people(data)  # must not raise


def test_a_bad_date_is_rejected():
    data = _base()
    data["donators"][0]["date"] = "01/01/2026"
    with pytest.raises(ValueError, match="date"):
        validate_people(data)


def test_a_non_http_avatar_is_rejected():
    data = _base()
    data["donators"][0]["avatar"] = "javascript:alert(1)"
    with pytest.raises(ValueError, match="avatar"):
        validate_people(data)


def test_an_optional_locale_field_left_entirely_blank_is_dropped_not_required():
    data = _base()
    result = validate_people(data)
    assert "comment" not in result["donators"][0]


def test_a_locale_field_with_only_ru_filled_saves_fine():
    """The bulk-import path (see the JS import handler in build_content.py)
    only ever fills RU - if this required both locales, every freshly
    imported row would be unsavable until hand-translated to English."""
    data = _base()
    data["donators"][0]["comment"] = {"ru": "Спасибо", "en": ""}
    result = validate_people(data)
    assert result["donators"][0]["comment"] == {"ru": "Спасибо", "en": ""}


def test_a_required_locale_field_still_needs_ru():
    data = _base()
    data["bughunters"][0]["bugTitle"] = {"ru": "", "en": "Title"}
    with pytest.raises(ValueError, match="bugTitle"):
        validate_people(data)


def test_icon_filters_are_stripped_along_with_the_layers_that_used_them():
    """A motion trace of the real app put the Info screen's first paint at a
    200ms frame with the renderer's main thread idle throughout - filter
    rasterisation does not run there. Screens without filters, including one
    with twice the pixel area, painted clean. At 18px none of these effects
    are visible anyway, so they are removed on the way in."""
    markup = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 40 50">'
        '<defs>'
        '<filter id="blur"><feGaussianBlur stdDeviation="3" /></filter>'
        '<path id="p" d="M0,0 L10,10 Z" />'
        '</defs>'
        '<g><use href="#p" filter="url(#blur)" fill="#000" />'
        '<use href="#p" fill="#EFA875" /></g>'
        '</svg>'
    )

    out = build_content.strip_svg_filters(markup)

    assert "feGaussianBlur" not in out
    assert "<filter" not in out
    # The blurred copy goes too: without its filter it is not a neutral
    # leftover but a hard-edged black duplicate over the real shape.
    assert out.count("use") == 1
    assert "#EFA875" in out


def test_an_icon_with_no_filters_is_left_byte_for_byte_alone():
    """Most icons have nothing to strip, and re-serialising them through an
    XML parser would churn about.json for no reason."""
    markup = '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0,0 L1,1" fill="#fff" /></svg>'

    assert build_content.strip_svg_filters(markup) == markup


def test_an_icon_that_is_nothing_but_a_filter_is_rejected_rather_than_emptied():
    """Stripping is only safe while something visible survives it. If nothing
    does, that is a drawing to redo by hand, not a blank button to ship."""
    markup = (
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<filter id="b"><feGaussianBlur stdDeviation="2" /></filter>'
        '<rect width="10" height="10" filter="url(#b)" />'
        '</svg>'
    )

    with pytest.raises(ValueError, match="ничего видимого"):
        build_content.strip_svg_filters(markup)


def test_the_shipped_icons_carry_no_filters():
    """Regression guard on the content itself, not just the importer - the
    icon that caused this shipped in about.json long before the importer knew
    to strip it."""
    about = json.loads(
        (Path(__file__).resolve().parents[2] / "frontend/src/data/content/about.json")
        .read_text(encoding="utf-8")
    )

    for link in about.get("links", []):
        svg = link.get("iconSvg") or ""
        assert "<filter" not in svg, f"{link['id']} still carries an SVG filter"
        assert "feGaussianBlur" not in svg, f"{link['id']} still carries a blur"
