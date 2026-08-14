"""Carrying profiles between machines.

Import is UNTRUSTED input that decides where the game's traffic - room tokens
included - is sent, so it is treated like the zapret archive in
zapret/update.py rather than like a settings form: everything goes through
pydantic, nothing overwrites what is already here, and the rules below are
tests rather than intentions.
"""
import json

import pytest

from bridgebox.config import ProfilesConfig
from bridgebox.profiles_io import MAX_IMPORT_PROFILES, export_payload, import_payload


def _custom(**over):
    base = {"id": "mine", "name": "Mine", "kind": "blobcast", "upstream": "https://mine.example"}
    base.update(over)
    return base


def test_export_carries_the_custom_profiles_and_not_the_builtins():
    """A built-in is defined by this build, not by whoever exported. Carrying
    them would mean importing somebody else's idea of the official address."""
    profiles = ProfilesConfig(items=list(ProfilesConfig().items) + [_custom()])

    payload = export_payload(profiles)

    assert [p["id"] for p in payload["profiles"]] == ["mine"]


def test_export_then_import_round_trips_into_a_clean_config():
    profiles = ProfilesConfig(items=list(ProfilesConfig().items) + [_custom()])

    imported, report = import_payload(json.dumps(export_payload(profiles)), into=ProfilesConfig())

    assert report["added"] == 1
    restored = [p for p in imported.items if not p.builtin]
    assert [p.name for p in restored] == ["Mine"]
    assert restored[0].upstream == "https://mine.example"


def test_an_imported_builtin_is_ignored_rather_than_applied():
    """Otherwise a shared file could silently repoint the official server and
    nothing on screen would say so."""
    raw = json.dumps(
        {"profiles": [{"id": "official-ecast", "name": "Totally official",
                       "kind": "ecast", "upstream": "https://evil.example", "builtin": True}]}
    )

    imported, report = import_payload(raw, into=ProfilesConfig())

    assert imported.active("ecast").upstream == "https://ecast.jackboxgames.com"
    assert report["skipped"], "the skip has to be reported, not silent"


def test_an_id_collision_gets_a_new_id_instead_of_overwriting():
    existing = ProfilesConfig(items=list(ProfilesConfig().items) + [_custom(name="Original")])
    raw = json.dumps({"profiles": [_custom(name="Incoming")]})

    imported, report = import_payload(raw, into=existing)

    names = sorted(p.name for p in imported.items if not p.builtin)
    assert names == ["Incoming", "Original"], "both survive; nothing is replaced"
    assert len({p.id for p in imported.items}) == len(imported.items)
    assert report["added"] == 1


def test_the_active_selection_in_the_file_is_ignored():
    """A file from someone else must not decide where YOUR traffic goes. It
    may add destinations; choosing one stays a deliberate act."""
    raw = json.dumps({"profiles": [_custom()], "active_blobcast": "mine"})

    imported, _ = import_payload(raw, into=ProfilesConfig())

    assert imported.active("blobcast").id == "official-blobcast"


@pytest.mark.parametrize(
    "bad",
    [
        {"profiles": [_custom(upstream="http://plain.example")]},
        {"profiles": [_custom(kind="nonsense")]},
        {"profiles": [_custom(upstream="")]},
    ],
)
def test_an_invalid_profile_is_skipped_and_the_rest_still_import(bad):
    raw = json.dumps({"profiles": bad["profiles"] + [_custom(id="ok", name="Fine")]})

    imported, report = import_payload(raw, into=ProfilesConfig())

    assert [p.name for p in imported.items if not p.builtin] == ["Fine"]
    assert len(report["skipped"]) == 1


def test_too_many_profiles_is_refused():
    raw = json.dumps(
        {"profiles": [_custom(id=f"p{i}", name=f"P{i}") for i in range(MAX_IMPORT_PROFILES + 1)]}
    )

    with pytest.raises(ValueError, match="слишком"):
        import_payload(raw, into=ProfilesConfig())


@pytest.mark.parametrize("raw", ["not json", "[]", '{"profiles": "nope"}', ""])
def test_junk_is_rejected_with_a_message_rather_than_a_traceback(raw):
    with pytest.raises(ValueError):
        import_payload(raw, into=ProfilesConfig())
