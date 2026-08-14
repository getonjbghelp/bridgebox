"""Carrying connection profiles between machines.

Import is UNTRUSTED input that decides where the game's traffic - room tokens
included - gets sent, so it is handled like the zapret archive in
zapret/update.py rather than like a settings form. Four rules, each of them a
test in test_profiles_io.py:

- everything goes through pydantic, so an upstream that is not https, a kind
  that is not a kind, or a port outside the range never lands;
- built-ins are never imported. A built-in is defined by this build, not by
  whoever exported the file; accepting one would let a shared file repoint the
  official server with nothing on screen to say so;
- an id collision produces a NEW id rather than replacing what is already
  here. Import adds, it never silently overwrites;
- the active selection in the file is ignored. A file from someone else may
  offer destinations; choosing one stays a deliberate act by the person whose
  traffic it is.

Both functions are pure. The file dialogs live in desktop.py, so the rules
above are testable without touching a filesystem.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import ValidationError

from .config import Profile, ProfilesConfig

logger = logging.getLogger(__name__)

FORMAT = "bridgebox-profiles"
FORMAT_VERSION = 1

# Generous next to any plausible real use, tight enough that a hostile or
# corrupt file cannot bloat config.yaml. Same reasoning as update.MAX_MEMBERS.
MAX_IMPORT_PROFILES = 200
MAX_IMPORT_CHARS = 1_000_000


def export_payload(profiles: ProfilesConfig) -> dict[str, Any]:
    """Everything worth carrying: the user's own profiles, with their
    settings. Built-ins are excluded - see the module docstring."""
    return {
        "format": FORMAT,
        "version": FORMAT_VERSION,
        "profiles": [p.model_dump() for p in profiles.items if not p.builtin],
    }


def _unique_id(wanted: str, taken: set[str]) -> str:
    if wanted not in taken:
        return wanted
    for suffix in range(2, 1000):
        candidate = f"{wanted}-{suffix}"
        if candidate not in taken:
            return candidate
    raise ValueError("не удалось подобрать свободный идентификатор профиля")


def import_payload(
    raw: str | dict[str, Any], *, into: ProfilesConfig
) -> tuple[ProfilesConfig, dict[str, Any]]:
    """Add the file's profiles to `into`, returning a new config and a report.

    The report is returned rather than logged only, because "12 imported, 3
    skipped" is exactly the thing a user needs on screen - a silent partial
    import looks identical to a broken file."""
    if isinstance(raw, str):
        if len(raw) > MAX_IMPORT_CHARS:
            raise ValueError("файл слишком большой для списка профилей")
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError(f"это не похоже на JSON: {exc}") from exc
    else:
        data = raw

    if not isinstance(data, dict):
        raise ValueError("ожидался объект JSON с полем «profiles»")
    incoming = data.get("profiles")
    if not isinstance(incoming, list):
        raise ValueError("в файле нет списка «profiles»")
    if len(incoming) > MAX_IMPORT_PROFILES:
        raise ValueError(f"слишком много профилей: {len(incoming)}")

    items = [p.model_copy(deep=True) for p in into.items]
    taken = {p.id for p in items}
    added = 0
    skipped: list[dict[str, str]] = []

    for entry in incoming:
        if not isinstance(entry, dict):
            skipped.append({"name": str(entry)[:40], "reason": "не объект"})
            continue
        entry = dict(entry)
        name = str(entry.get("name") or entry.get("id") or "?")[:60]
        if entry.get("builtin"):
            skipped.append({"name": name, "reason": "встроенный профиль не импортируется"})
            continue

        entry["builtin"] = False
        entry["id"] = _unique_id(str(entry.get("id") or "imported"), taken)
        try:
            profile = Profile.model_validate(entry)
        except ValidationError as exc:
            first = exc.errors()[0]
            skipped.append({"name": name, "reason": str(first.get("msg", "не прошёл проверку"))})
            continue

        items.append(profile)
        taken.add(profile.id)
        added += 1

    # active_ecast/active_blobcast are deliberately carried over from `into`
    # and never taken from the file.
    result = ProfilesConfig(
        items=items, active_ecast=into.active_ecast, active_blobcast=into.active_blobcast
    )
    logger.info("imported %d profiles, skipped %d", added, len(skipped))
    return result, {"added": added, "skipped": skipped}
