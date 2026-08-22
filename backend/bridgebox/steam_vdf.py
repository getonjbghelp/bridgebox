"""Surgical text edits to one appid's LaunchOptions inside a Steam
localconfig.vdf-shaped string - never a full parse/reserialize of the
whole file.

localconfig.vdf holds every setting for a Steam account (friends, cloud
sync, every installed game), not just this one field. Rewriting the whole
tree through a general KeyValues library risks reformatting whitespace or
quoting in places this has no business touching. Brace-counting is enough:
VDF nesting is regular, and the only structure this needs to understand is
"where does the <appid> block start and end" - not the full grammar.
"""
from __future__ import annotations

import re

_LAUNCH_OPTIONS_RE = re.compile(r'("LaunchOptions"\s*)"((?:[^"\\]|\\.)*)"')


class AppBlockNotFound(Exception):
    """No `"<appid>"` key exists in this file at all. Callers must treat
    this as "not eligible" - never as "create one from scratch"."""


def _unescape(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


_APPS_KEY_RE = re.compile(r'"apps"\s*\n?\s*\{')


def _find_apps_block(vdf_text: str) -> tuple[int, int]:
    """(start, end) offsets of the `"apps" { ... }` block's BODY.
    localconfig.vdf has other numeric-keyed subtrees outside "apps" (e.g.
    per-friend or per-tool blocks) - without this, a search for an appid
    over the WHOLE file could match a same-numbered key from one of those
    subtrees instead of the actual game."""
    match = _APPS_KEY_RE.search(vdf_text)
    if match is None:
        raise AppBlockNotFound("apps")
    depth = 1
    i = match.end()
    while i < len(vdf_text) and depth > 0:
        if vdf_text[i] == "{":
            depth += 1
        elif vdf_text[i] == "}":
            depth -= 1
        i += 1
    if depth != 0:
        raise AppBlockNotFound("apps")
    return match.end(), i - 1


def _find_app_block(vdf_text: str, appid: str) -> tuple[int, int]:
    """(start, end) offsets of the `"<appid>" { ... }` block's BODY, scoped
    to within the "apps" block - the text strictly between its opening and
    matching closing brace. Offsets are translated back to positions in the
    full text so callers can slice vdf_text directly."""
    apps_start, apps_end = _find_apps_block(vdf_text)
    apps_text = vdf_text[apps_start:apps_end]
    key_pattern = re.compile(rf'"{re.escape(appid)}"\s*\n?\s*\{{')
    match = key_pattern.search(apps_text)
    if match is None:
        raise AppBlockNotFound(appid)
    depth = 1
    i = match.end()
    while i < len(apps_text) and depth > 0:
        if apps_text[i] == "{":
            depth += 1
        elif apps_text[i] == "}":
            depth -= 1
        i += 1
    if depth != 0:
        raise AppBlockNotFound(appid)  # malformed file - refuse rather than guess
    return apps_start + match.end(), apps_start + i - 1


def read_launch_options(vdf_text: str, appid: str) -> str | None:
    """The current LaunchOptions value, or None if appid has no block at
    all (never launched through Steam) - distinct from "" (block exists,
    LaunchOptions was never set)."""
    try:
        start, end = _find_app_block(vdf_text, appid)
    except AppBlockNotFound:
        return None
    match = _LAUNCH_OPTIONS_RE.search(vdf_text[start:end])
    return _unescape(match.group(2)) if match else ""


def set_launch_options(vdf_text: str, appid: str, value: str) -> str:
    """vdf_text with appid's LaunchOptions set to value - every other byte,
    including sibling apps and unrelated settings, unchanged. Raises
    AppBlockNotFound if appid has no block; callers must exclude such games
    upstream rather than relying on this to create one."""
    start, end = _find_app_block(vdf_text, appid)
    block = vdf_text[start:end]
    escaped = _escape(value)
    if _LAUNCH_OPTIONS_RE.search(block):
        new_block = _LAUNCH_OPTIONS_RE.sub(
            lambda m: f'{m.group(1)}"{escaped}"', block, count=1
        )
    else:
        indent_match = re.search(r"\n(\t+)\"", block)
        indent = indent_match.group(1) if indent_match else "\t\t\t\t\t"
        new_block = f'\n{indent}"LaunchOptions"\t\t"{escaped}"' + block
    return vdf_text[:start] + new_block + vdf_text[end:]
