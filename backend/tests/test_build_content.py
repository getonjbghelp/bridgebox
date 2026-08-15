"""validate_people() - the one piece of tools/build_content.py's people.json
support worth a regression test. Everything else in that script is HTTP
plumbing or generated HTML, exercised by running the editor, not a unit
test standing in for one."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

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
    }
    data.update(overrides)
    return data


def test_a_minimal_valid_entry_per_category_round_trips():
    result = validate_people(_base())
    assert result["donators"][0]["name"] == "Alice"
    assert result["bughunters"][0]["bugTitle"] == {"ru": "Заголовок", "en": "Title"}
    assert result["testers"][0]["environment"] == "Windows 11"


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
