"""Local editor for BridgeBox's data-driven content: the "about" page
(frontend/src/data/content/about.json) shown on the Info screen, and the
"Спасибо" people module (people.json).

The version history used to live here too (changelog.json, hand-edited
through a tab in this same tool) - it is now fetched straight from GitHub
Releases at runtime instead (see frontend/src/lib/changelog.ts), because the
"single BridgeBox source of truth" for a release's own notes is the release
itself, not a second file someone has to remember to update in lockstep. What
survives locally is legacyChangelog.json - the frozen pre-0.1.6 entries that
predate the GitHub convention and were never going to gain new ones - kept as
a plain static file, not something this tool edits any more.

Run it, a browser tab opens - the same kind of tool as edit_ui_strings.py, and
for the same reason: hand-editing JSON files that have to stay internally
consistent (RU/EN in lockstep, a link's icon naming something that actually
exists) is exactly the class of mistake a small local editor exists to catch
before it ships.

    python tools/build_content.py

stdlib only, no dependencies to install. Binds to 127.0.0.1 on an OS-chosen
free port so it never collides with anything else running locally.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
import sys
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
CONTENT_DIR = FRONTEND_SRC / "data" / "content"
ABOUT_PATH = CONTENT_DIR / "about.json"
PEOPLE_PATH = CONTENT_DIR / "people.json"
ICONS_PATH = FRONTEND_SRC / "components" / "icons.tsx"
LOGO_PATH = FRONTEND_SRC / "components" / "BrandLogo.tsx"

LOCALES = ("ru", "en")
PEOPLE_CATEGORIES = ("donators", "bughunters", "testers", "other")

_VIEWBOX_RE = re.compile(r'viewBox="([\d.\s-]+)"')
_PATH_D_RE = re.compile(r'd="(M[^"]+)"')
_LINK_ICONS_RE = re.compile(r"export const LINK_ICONS = \{(.*?)\n\}", re.DOTALL)
_ICON_KEY_RE = re.compile(r"(\w+):\s*Icon\w+")


def known_icons() -> list[str]:
    """The exact keys about.json's `links[].icon` is allowed to use - read out
    of icons.tsx's own LINK_ICONS registry rather than duplicated here by
    hand, so a new icon added to the app shows up here for free and a typo'd
    icon name is impossible to save."""
    source = ICONS_PATH.read_text(encoding="utf-8")
    block = _LINK_ICONS_RE.search(source)
    if not block:
        return []
    return _ICON_KEY_RE.findall(block.group(1))


def read_wordmark() -> str:
    """Same technique as edit_ui_strings.py's copy of this function - lifted
    from BrandLogo.tsx rather than duplicated as a second asset that could
    drift from the real one."""
    try:
        source = LOGO_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""
    viewbox = _VIEWBOX_RE.search(source)
    paths = _PATH_D_RE.findall(source)
    if not viewbox or not paths:
        return ""
    glyphs = "".join(f'<path d="{d}"/>' for d in paths)
    return (
        f'<svg class="logo" viewBox="{viewbox.group(1)}" role="img" '
        f'aria-label="BridgeBox" focusable="false">{glyphs}</svg>'
    )


def load_content() -> dict:
    about = json.loads(ABOUT_PATH.read_text(encoding="utf-8"))
    if PEOPLE_PATH.exists():
        people = json.loads(PEOPLE_PATH.read_text(encoding="utf-8"))
    else:
        # A checkout from before this file existed - start empty rather than
        # making the whole editor refuse to run over one missing category.
        people = {category: [] for category in PEOPLE_CATEGORIES}
    return {"about": about, "people": people}


def build_meta() -> dict:
    return {
        "knownIcons": known_icons(),
    }


SVG_NS = "http://www.w3.org/2000/svg"

_SVG_SCRIPT_RE = re.compile(r"<script", re.IGNORECASE)
_SVG_EVENT_ATTR_RE = re.compile(r"\son\w+\s*=", re.IGNORECASE)


def _is_safe_svg(markup: str) -> bool:
    """Just enough of a gate for a locally-authored icon, not a general HTML
    sanitizer: this markup ends up in about.json and is rendered with
    dangerouslySetInnerHTML (InfoScreen.tsx's LinkIcon), so the one thing
    that actually matters is that it can't carry a <script> or an inline
    event handler. It's still the developer's own machine writing this file,
    same trust level as every other string this tool saves."""
    stripped = markup.strip()
    if not stripped.lower().startswith("<svg"):
        return False
    if _SVG_SCRIPT_RE.search(stripped):
        return False
    if _SVG_EVENT_ATTR_RE.search(stripped):
        return False
    return True


def strip_svg_filters(markup: str) -> str:
    """Drop SVG filter effects from an icon, and the elements that use them.

    These icons render at 18px on the Info screen, where a Gaussian blur is
    somewhere under two pixels wide and an offset drop shadow under one - both
    invisible. The rasteriser still does the work, and the cost is not small:
    the Donatty icon shipped with two filter chains (a blurred glow and a
    four-stage feOffset/feGaussianBlur/feComposite/feColorMatrix shadow) over
    a path drawn three times, and a motion trace of the real app pinned the
    Info screen's first paint at a 200ms frame - with the renderer's own main
    thread idle the whole time, because filter rasterisation does not happen
    there. Every other screen, including one with twice the pixel area and
    more elements, painted clean. Filters were the only thing that set this
    screen apart.

    The elements that reference a filter go with it rather than just losing
    the attribute: an unfiltered copy of a shape that only existed to be
    blurred is not a neutral leftover, it is a hard-edged duplicate drawn over
    the real one (the black shadow layer here would render as a solid
    silhouette). What stays is what was actually visible at icon size."""
    try:
        ET.register_namespace("", SVG_NS)
        root = ET.fromstring(markup)
    except ET.ParseError:
        # Not parseable as XML - leave it exactly as the author wrote it and
        # let _is_safe_svg be the only gate, same as before this existed.
        return markup

    removed = 0
    for parent in root.iter():
        for child in list(parent):
            uses_filter = "url(#" in (child.get("filter") or "")
            if child.tag == f"{{{SVG_NS}}}filter" or uses_filter:
                parent.remove(child)
                removed += 1
    if removed == 0:
        return markup

    drawable = {"path", "use", "rect", "circle", "ellipse", "polygon", "polyline", "line", "text"}
    if not any(el.tag.split("}")[-1] in drawable for el in root.iter()):
        raise ValueError(
            "после удаления фильтров в иконке не осталось ничего видимого - "
            "перерисуйте её без filter/feGaussianBlur"
        )

    return ET.tostring(root, encoding="unicode")


def _is_valid_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme == "mailto":
        return bool(parsed.path)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def validate_about(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("about должен быть объектом")

    for locale in LOCALES:
        section = data.get(locale)
        if not isinstance(section, dict):
            raise ValueError(f"раздел {locale!r} обязателен")
        if not isinstance(section.get("description"), str) or not section["description"].strip():
            raise ValueError(f"{locale}: описание не может быть пустым")
        license_ = section.get("license")
        if not isinstance(license_, dict):
            raise ValueError(f"{locale}: license обязателен")
        if not isinstance(license_.get("name"), str) or not license_["name"].strip():
            raise ValueError(f"{locale}: license.name не может быть пустым")
        if not isinstance(license_.get("text"), str) or not license_["text"].strip():
            raise ValueError(f"{locale}: license.text не может быть пустым")

    links = data.get("links")
    if not isinstance(links, list):
        raise ValueError("links должен быть списком")
    icons = set(known_icons())
    seen_ids: set[str] = set()
    for i, link in enumerate(links):
        if not isinstance(link, dict):
            raise ValueError(f"ссылка {i + 1}: должна быть объектом")
        link_id = link.get("id")
        if not isinstance(link_id, str) or not link_id.strip():
            raise ValueError(f"ссылка {i + 1}: id не может быть пустым")
        if link_id in seen_ids:
            raise ValueError(f"id {link_id!r} повторяется")
        seen_ids.add(link_id)
        icon = link.get("icon")
        if icon == "custom":
            svg = link.get("iconSvg")
            if not isinstance(svg, str) or not _is_safe_svg(svg):
                raise ValueError(
                    f"{link_id}: iconSvg должен начинаться с <svg и не содержать "
                    f"<script> или обработчики событий (onXxx=)"
                )
            # Rewritten in place, so what lands in about.json is what the
            # Info screen will actually paint - see strip_svg_filters.
            link["iconSvg"] = strip_svg_filters(svg)
        elif icon not in icons:
            raise ValueError(
                f"{link_id}: иконка {icon!r} не найдена в icons.tsx и не 'custom' "
                f"(доступны: {', '.join(sorted(icons))})"
            )
        label = link.get("label")
        if not isinstance(label, dict):
            raise ValueError(f"{link_id}: label обязателен")
        for locale in LOCALES:
            if not isinstance(label.get(locale), str) or not label[locale].strip():
                raise ValueError(f"{link_id}: label.{locale} не может быть пустым")

        action = link.get("action")
        if action not in ("link", "popup"):
            raise ValueError(f"{link_id}: action должен быть 'link' или 'popup'")
        if action == "link":
            url = link.get("url")
            if not isinstance(url, str) or not _is_valid_url(url):
                raise ValueError(f"{link_id}: url должен быть http(s):// или mailto:")
        else:
            _require_locale_dict(link.get("popupTitle"), f"{link_id}: popupTitle")
            _require_locale_dict(link.get("popupText"), f"{link_id}: popupText")
            popup_url = link.get("popupUrl")
            if popup_url:
                if not isinstance(popup_url, str) or not _is_valid_url(popup_url):
                    raise ValueError(f"{link_id}: popupUrl должен быть http(s):// или mailto:")
                _require_locale_dict(link.get("popupUrlLabel"), f"{link_id}: popupUrlLabel")
    return data


def _require_text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} не может быть пустым")
    return value.strip()


# Which fields each category needs beyond id/name/avatar, and how to check
# them - a table rather than three near-identical if-branches, so a new
# required field is one line here instead of one in each of three places.
_PEOPLE_SCHEMA: dict[str, list[tuple[str, str, bool]]] = {
    # (field, kind, required) - kind is "text", "locale", or "url"
    "donators": [
        ("date", "date", True),
        ("platform", "text", True),
        ("amount", "text", False),
        ("comment", "locale", False),
    ],
    "bughunters": [
        ("bugTitle", "locale", True),
        ("bugDescription", "locale", True),
        ("link", "url", False),
    ],
    "testers": [
        ("tested", "locale", True),
        ("environment", "text", True),
        ("contribution", "locale", True),
    ],
    # Everyone who earned a thank-you for something that isn't a donation, a
    # bug report, or testing - one free-text field, deliberately not shaped
    # like the other three.
    "other": [
        ("reason", "locale", True),
    ],
}


def validate_people(data: object) -> dict:
    if not isinstance(data, dict):
        raise ValueError("people должен быть объектом")

    result: dict[str, list[dict]] = {}
    for category in PEOPLE_CATEGORIES:
        entries = data.get(category, [])
        if not isinstance(entries, list):
            raise ValueError(f"{category} должен быть списком")
        seen_ids: set[str] = set()
        validated: list[dict] = []
        for i, entry in enumerate(entries):
            what = f"{category}[{i + 1}]"
            if not isinstance(entry, dict):
                raise ValueError(f"{what}: должен быть объектом")
            entry_id = entry.get("id")
            if not isinstance(entry_id, str) or not entry_id.strip():
                raise ValueError(f"{what}: id не может быть пустым")
            if entry_id in seen_ids:
                raise ValueError(f"{category}: id {entry_id!r} повторяется")
            seen_ids.add(entry_id)
            name = _require_text(entry.get("name"), f"{what}: name")

            cleaned: dict = {"id": entry_id.strip(), "name": name}
            avatar = entry.get("avatar")
            if avatar:
                if not _is_valid_url(avatar):
                    raise ValueError(f"{what}: avatar должен быть http(s)://")
                cleaned["avatar"] = avatar

            for field, kind, required in _PEOPLE_SCHEMA[category]:
                value = entry.get(field)
                if kind == "text":
                    if required:
                        cleaned[field] = _require_text(value, f"{what}: {field}")
                    elif value:
                        cleaned[field] = str(value).strip()
                elif kind == "date":
                    text = _require_text(value, f"{what}: {field}")
                    try:
                        date.fromisoformat(text)
                    except ValueError:
                        raise ValueError(f"{what}: {field} {text!r} не в формате ГГГГ-ММ-ДД") from None
                    cleaned[field] = text
                elif kind == "url":
                    if value:
                        if not _is_valid_url(value):
                            raise ValueError(f"{what}: {field} должен быть http(s)://")
                        cleaned[field] = value
                elif kind == "locale":
                    # RU is the one locale this ever requires, matching the
                    # rest of the content pipeline (aboutText/changelogText's
                    # "a translation not written yet reads as Russian rather
                    # than blank") - a bulk import only ever fills RU (see
                    # the JS import handler), so requiring EN too would make
                    # every bulk-imported row unsavable until hand-translated.
                    ru_text = value.get("ru", "").strip() if isinstance(value, dict) else ""
                    if required and not ru_text:
                        raise ValueError(f"{what}: {field}.ru не может быть пустым")
                    if ru_text:
                        en_value = value.get("en", "") if isinstance(value, dict) else ""
                        cleaned[field] = {
                            "ru": ru_text,
                            "en": en_value.strip() if isinstance(en_value, str) else "",
                        }
            validated.append(cleaned)
        result[category] = validated
    return result


def _require_locale_dict(value: object, what: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{what} обязателен")
    for locale in LOCALES:
        if not isinstance(value.get(locale), str) or not value[locale].strip():
            raise ValueError(f"{what}.{locale} не может быть пустым")


def _atomic_write(path: Path, data: object) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def save_about(data: object) -> None:
    validated = validate_about(data)
    _atomic_write(ABOUT_PATH, validated)


def save_people(data: object) -> None:
    validated = validate_people(data)
    _atomic_write(PEOPLE_PATH, validated)


def render_page() -> str:
    return (
        PAGE_TEMPLATE.replace("__CONTENT_JSON__", json.dumps(load_content(), ensure_ascii=False))
        .replace("__META_JSON__", json.dumps(build_meta(), ensure_ascii=False))
        .replace("__WORDMARK__", read_wordmark())
    )


# Same palette and layout language as edit_ui_strings.py - two local tools a
# developer flips between should not look like two different products.
PAGE_TEMPLATE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>BridgeBox — редактор контента</title>
<style>
  :root {
    --bg: #f4f6fa; --surface: #ffffff; --sunken: #eef1f6;
    --border: #e2e7ef; --border-strong: #cbd4e1;
    --text: #0b1220; --text-2: #5b6472; --text-3: #8b93a1;
    --accent: #2563eb; --accent-soft: #e8f0fe;
    --success: #15803d; --success-soft: #dcfce7;
    --danger: #b91c1c; --danger-soft: #fdecec;
    --warn: #92400e; --warn-soft: #fef3c7;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0d1117; --surface: #151b24; --sunken: #1c232e;
      --border: #262d38; --border-strong: #38414f;
      --text: #e8eef8; --text-2: #a3adbd; --text-3: #7c8797;
      --accent: #6ea8fe; --accent-soft: #1b2a44;
      --success: #4ade80; --success-soft: #16301f;
      --danger: #f87171; --danger-soft: #3a1b1b;
      --warn: #fbbf24; --warn-soft: #3a2f12;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 15px/1.6 -apple-system, "Segoe UI", system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  header {
    position: sticky; top: 0; z-index: 10;
    display: flex; align-items: center; gap: 24px; padding: 18px 36px;
    background: var(--surface); border-bottom: 1px solid var(--border);
    box-shadow: 0 1px 0 rgb(15 23 42 / 3%);
  }
  h1 { font-size: 19px; font-weight: 700; margin: 0; white-space: nowrap; }
  .brand { display: flex; align-items: center; gap: 12px; }
  .logo { height: 22px; width: auto; fill: currentColor; color: var(--text); display: block; }
  main { max-width: 960px; margin: 0 auto; padding: 36px 36px 120px; }
  .tabs { display: flex; gap: 6px; background: var(--sunken); padding: 4px; border-radius: 12px; }
  .tab {
    font: inherit; font-size: 13.5px; font-weight: 600; padding: 8px 18px; border-radius: 9px;
    border: none; cursor: pointer; background: transparent; color: var(--text-2);
    transition: background .15s ease, color .15s ease;
  }
  .tab:hover { color: var(--text); }
  .tab.active { background: var(--surface); color: var(--accent); box-shadow: 0 1px 2px rgb(15 23 42 / 8%); }
  /* Same look as .tab/.tabs, separate class: the people panel's category
     switcher must not be found by the outer header-tab click handler, which
     assumes every ".tab" it sees has a "panel" it can toggle. */
  .subtabs { display: flex; gap: 6px; background: var(--sunken); padding: 4px; border-radius: 12px; margin-bottom: 20px; }
  .subtab {
    font: inherit; font-size: 13.5px; font-weight: 600; padding: 8px 18px; border-radius: 9px;
    border: none; cursor: pointer; background: transparent; color: var(--text-2);
    transition: background .15s ease, color .15s ease;
  }
  .subtab:hover { color: var(--text); }
  .subtab.active { background: var(--surface); color: var(--accent); box-shadow: 0 1px 2px rgb(15 23 42 / 8%); }
  .subpanel { display: none; }
  .subpanel.active { display: block; }
  details.bulk-import { margin-bottom: 20px; }
  details.bulk-import > summary { cursor: pointer; font-weight: 600; font-size: 13.5px; color: var(--text-2); padding: 4px 0; }
  details.bulk-import textarea { margin-top: 10px; min-height: 110px; font: 13px/1.5 ui-monospace, monospace; }
  #status { margin-left: auto; font-size: 13px; font-weight: 600; color: var(--text-3); transition: color .15s ease; }
  #status.ok { color: var(--success); }
  #status.err { color: var(--danger); }
  button { font: inherit; }
  button.primary {
    font-weight: 600; padding: 11px 24px; border-radius: 11px; font-size: 14px;
    border: none; cursor: pointer; background: var(--accent); color: #fff;
    box-shadow: 0 1px 2px rgb(15 23 42 / 10%);
    transition: opacity .15s ease, transform .1s ease;
  }
  button.primary:hover { opacity: 0.92; }
  button.primary:active { transform: scale(0.98); }
  button.ghost {
    font-weight: 600; padding: 9px 18px; border-radius: 10px; font-size: 13.5px;
    border: 1px solid var(--border-strong); cursor: pointer; background: transparent; color: var(--text-2);
    transition: border-color .15s ease, color .15s ease;
  }
  button.ghost:hover { border-color: var(--danger); color: var(--danger); }
  button.save-bar { margin-top: 24px; }
  .card {
    background: var(--surface); border: 1px solid var(--border); border-radius: 16px;
    box-shadow: 0 1px 3px rgb(15 23 42 / 4%);
    padding: 24px 26px; margin-bottom: 20px;
  }
  .card-head {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding-bottom: 16px; margin-bottom: 20px; border-bottom: 1px solid var(--border);
  }
  .card-head strong { font-size: 15px; font-weight: 700; }
  .card-head .meta { font-size: 12px; color: var(--text-3); }
  .row { display: grid; grid-template-columns: 160px 1fr; gap: 14px; align-items: start; margin-bottom: 16px; }
  .row:last-child { margin-bottom: 0; }
  .row label { font-size: 13px; font-weight: 600; color: var(--text-2); padding-top: 12px; }
  .row-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .lang-tag {
    font-size: 11px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase;
    color: var(--text-3); display: flex; align-items: center; gap: 6px; margin-bottom: 8px;
  }
  .lang-tag::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: var(--border-strong); }
  input, select, textarea {
    width: 100%; font: inherit; font-size: 14.5px; padding: 11px 14px; border: 1.5px solid var(--border-strong);
    border-radius: 10px; background: var(--surface); color: var(--text);
    transition: border-color .15s ease, box-shadow .15s ease;
  }
  input:focus, select:focus, textarea:focus {
    outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft);
  }
  textarea { resize: vertical; min-height: 90px; line-height: 1.55; }
  textarea.tall { min-height: 140px; }
  .badge {
    font-size: 11px; padding: 3px 10px; border-radius: 999px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.03em;
  }
  .badge--minor { background: var(--sunken); color: var(--text-2); }
  .badge--major { background: var(--success-soft); color: var(--success); }
  .badge--critical { background: var(--danger-soft); color: var(--danger); }
  .problem {
    color: var(--danger); background: var(--danger-soft); font-size: 13.5px; font-weight: 500;
    margin: 16px 0 0; padding: 12px 16px; border-radius: 10px; white-space: pre-wrap;
  }
  .hint { color: var(--text-2); font-size: 13.5px; line-height: 1.6; margin: 0 0 20px; max-width: 68ch; }
  section.panel { display: none; }
  section.panel.active { display: block; }
  .section-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin: 32px 0 6px; }
  .section-head strong { font-size: 16px; font-weight: 700; }
  .action-block {
    display: none; margin-top: 16px; padding: 18px 20px; border-radius: 12px;
    background: var(--sunken); border: 1px solid var(--border);
  }
  .action-block.active { display: block; }
  .subtle { color: var(--text-3); font-size: 12.5px; margin: 6px 0 0; }
  .add-card {
    display: flex; align-items: center; justify-content: center; gap: 8px; width: 100%;
    padding: 16px; margin-top: 4px; font-weight: 600; font-size: 14px; color: var(--text-2);
    background: transparent; border: 1.5px dashed var(--border-strong); border-radius: 14px;
    cursor: pointer; transition: border-color .15s ease, color .15s ease, background .15s ease;
  }
  .add-card:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-soft); }
  .icon-picker { display: flex; align-items: center; gap: 12px; }
  .icon-picker select { flex: 1; }
  .icon-preview {
    flex: none; width: 44px; height: 44px; display: flex; align-items: center; justify-content: center;
    border: 1.5px solid var(--border-strong); border-radius: 10px; background: var(--surface); color: var(--text);
  }
  .icon-preview svg { width: 22px; height: 22px; }
  input[type="file"] { padding: 8px 10px; cursor: pointer; }
</style>
</head>
<body>
<header>
  <div class="brand">__WORDMARK__<h1>Контент</h1></div>
  <div class="tabs">
    <button class="tab active" data-panel="about">О программе</button>
    <button class="tab" data-panel="people">Благодарности</button>
  </div>
  <span id="status"></span>
</header>
<main>

  <section id="panel-about" class="panel active">
    <div class="card">
      <div class="card-head"><strong>Описание и лицензия</strong></div>
      <div class="row-pair">
        <div><span class="lang-tag">RU — описание</span><textarea class="tall" id="about-ru-description"></textarea></div>
        <div><span class="lang-tag">EN — description</span><textarea class="tall" id="about-en-description"></textarea></div>
      </div>
      <div class="row-pair" style="margin-top:20px">
        <div><span class="lang-tag">RU — лицензия (название)</span><input id="about-ru-license-name"></div>
        <div><span class="lang-tag">EN — license (name)</span><input id="about-en-license-name"></div>
      </div>
      <div class="row-pair" style="margin-top:20px">
        <div><span class="lang-tag">RU — текст лицензии</span><textarea id="about-ru-license-text"></textarea></div>
        <div><span class="lang-tag">EN — license text</span><textarea id="about-en-license-text"></textarea></div>
      </div>
    </div>

    <div class="section-head"><strong>Ссылки</strong></div>
    <p class="hint">Иконка выбирается из набора, который реально есть в icons.tsx — опечатка невозможна. Каждая ссылка — либо прямой переход по URL, либо всплывающее окно с текстом (и, по желанию, своей ссылкой внутри).</p>
    <div id="links"></div>
    <button class="add-card" id="add-link" type="button">+ Добавить ссылку</button>
    <button class="primary save-bar" id="save-about">Сохранить «О программе»</button>
    <p class="problem" id="about-problem" hidden></p>
  </section>

  <section id="panel-people" class="panel">
    <p class="hint">Донатеры, багхантеры, тестеры и прочие благодарности, показанные на экране «Инфо». Каждая карточка — один человек; массовый импорт ниже добавляет карточки из вставленной таблицы, а не заменяет уже существующие.</p>
    <div class="subtabs" id="people-subtabs">
      <button class="subtab active" data-sub="donators">Донатеры</button>
      <button class="subtab" data-sub="bughunters">Багхантеры</button>
      <button class="subtab" data-sub="testers">Тестеры</button>
      <button class="subtab" data-sub="other">Прочее</button>
    </div>

    <div class="subpanel active" data-sub-panel="donators">
      <details class="bulk-import">
        <summary>Массовый импорт из таблицы (TSV/CSV)</summary>
        <p class="subtle">Одна строка — один человек. Столбцы: Ник; Дата (ГГГГ-ММ-ДД); Платформа; Сумма (необязательно); Комментарий на русском (необязательно). Разделитель — таб (при вставке из таблицы) или точка с запятой.</p>
        <textarea id="bulk-donators" placeholder="Ник;2026-01-01;Donatty;500 ₽;Спасибо за донат!"></textarea>
        <button class="ghost" id="import-donators" type="button" style="margin-top:10px">Импортировать строки</button>
      </details>
      <div id="list-donators"></div>
      <button class="add-card" id="add-donators" type="button">+ Добавить донатера</button>
    </div>

    <div class="subpanel" data-sub-panel="bughunters">
      <details class="bulk-import">
        <summary>Массовый импорт из таблицы (TSV/CSV)</summary>
        <p class="subtle">Столбцы: Ник; Заголовок бага на русском; Описание на русском; Ссылка на issue/коммит (необязательно).</p>
        <textarea id="bulk-bughunters" placeholder="Ник;Белая шапка окна на Windows 10;Нашёл рассинхронизацию темы;https://github.com/.../issues/1"></textarea>
        <button class="ghost" id="import-bughunters" type="button" style="margin-top:10px">Импортировать строки</button>
      </details>
      <div id="list-bughunters"></div>
      <button class="add-card" id="add-bughunters" type="button">+ Добавить багхантера</button>
    </div>

    <div class="subpanel" data-sub-panel="testers">
      <details class="bulk-import">
        <summary>Массовый импорт из таблицы (TSV/CSV)</summary>
        <p class="subtle">Столбцы: Ник; Что тестировал(а) на русском; Конфигурация/ОС; Ключевой вклад на русском.</p>
        <textarea id="bulk-testers" placeholder="Ник;Мастер настройки;Windows 10 22H2;Отловил баг с шапкой окна"></textarea>
        <button class="ghost" id="import-testers" type="button" style="margin-top:10px">Импортировать строки</button>
      </details>
      <div id="list-testers"></div>
      <button class="add-card" id="add-testers" type="button">+ Добавить тестера</button>
    </div>

    <div class="subpanel" data-sub-panel="other">
      <details class="bulk-import">
        <summary>Массовый импорт из таблицы (TSV/CSV)</summary>
        <p class="subtle">Столбцы: Ник; За что благодарность, на русском.</p>
        <textarea id="bulk-other" placeholder="Ник;Придумал название для функции автовставки"></textarea>
        <button class="ghost" id="import-other" type="button" style="margin-top:10px">Импортировать строки</button>
      </details>
      <div id="list-other"></div>
      <button class="add-card" id="add-other" type="button">+ Добавить запись</button>
    </div>

    <button class="primary save-bar" id="save-people">Сохранить благодарности</button>
    <p class="problem" id="people-problem" hidden></p>
  </section>
</main>
<script>
const CONTENT = __CONTENT_JSON__;
const META = __META_JSON__;

// ---- tabs -------------------------------------------------------------
document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach((p) => p.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('panel-' + tab.dataset.panel).classList.add('active');
  });
});

function setStatus(text, cls) {
  const el = document.getElementById('status');
  el.textContent = text;
  el.className = cls || '';
}

// ---- about ----------------------------------------------------------------

for (const locale of ['ru', 'en']) {
  document.getElementById(`about-${locale}-description`).value = CONTENT.about[locale].description;
  document.getElementById(`about-${locale}-license-name`).value = CONTENT.about[locale].license.name;
  document.getElementById(`about-${locale}-license-text`).value = CONTENT.about[locale].license.text;
}

const linksEl = document.getElementById('links');

function linkRow(link) {
  const row = document.createElement('div');
  row.className = 'card';
  const popupUrlLabel = link.popupUrlLabel || {};
  row.innerHTML = `
    <div class="card-head">
      <input class="f-id" placeholder="id (напр. telegram)" style="max-width:160px" value="${esc(link.id)}">
      <button class="ghost remove-link" type="button">Удалить</button>
    </div>
    <div class="row"><label>Иконка</label>
      <div class="icon-picker">
        <div class="icon-preview"></div>
        <select class="f-icon">
          ${META.knownIcons.map((i) => `<option value="${i}"${i === link.icon ? ' selected' : ''}>${i}</option>`).join('')}
          <option value="custom"${link.icon === 'custom' ? ' selected' : ''}>— свой SVG —</option>
        </select>
      </div>
    </div>
    <div class="action-block icon-custom-block">
      <div class="row"><label>Файл .svg</label><input class="f-icon-svg-file" type="file" accept=".svg,image/svg+xml"></div>
      <div class="row" style="margin-top:16px"><label>Код SVG</label><textarea class="f-icon-svg-code" placeholder="<svg viewBox=&quot;0 0 24 24&quot;>...</svg>">${esc(link.iconSvg || '')}</textarea></div>
      <p class="subtle" data-role="icon-svg-error"></p>
    </div>
    <div class="row"><label>Действие</label>
      <select class="f-action">
        <option value="link"${link.action !== 'popup' ? ' selected' : ''}>Открыть ссылку</option>
        <option value="popup"${link.action === 'popup' ? ' selected' : ''}>Показать попап</option>
      </select>
    </div>
    <div class="row-pair">
      <div><span class="lang-tag">RU — подсказка при наведении</span><input class="f-label-ru" value="${esc(link.label.ru || '')}"></div>
      <div><span class="lang-tag">EN — hover hint</span><input class="f-label-en" value="${esc(link.label.en || '')}"></div>
    </div>

    <div class="action-block block-link">
      <div class="row"><label>URL</label><input class="f-url" placeholder="https://… или mailto:…" value="${esc(link.url || '')}"></div>
    </div>

    <div class="action-block block-popup">
      <div class="row-pair">
        <div><span class="lang-tag">RU — заголовок попапа</span><input class="f-popup-title-ru" value="${esc((link.popupTitle || {}).ru || '')}"></div>
        <div><span class="lang-tag">EN — popup title</span><input class="f-popup-title-en" value="${esc((link.popupTitle || {}).en || '')}"></div>
      </div>
      <div class="row-pair" style="margin-top:16px">
        <div><span class="lang-tag">RU — текст (можно [текст](url), **жирный**)</span><textarea class="tall f-popup-text-ru">${esc((link.popupText || {}).ru || '')}</textarea></div>
        <div><span class="lang-tag">EN — popup text</span><textarea class="tall f-popup-text-en">${esc((link.popupText || {}).en || '')}</textarea></div>
      </div>
      <div class="row" style="margin-top:16px"><label>Ссылка в попапе</label><input class="f-popup-url" placeholder="необязательно" value="${esc(link.popupUrl || '')}"></div>
      <p class="subtle">Подпись ниже нужна, только если заполнена ссылка выше.</p>
      <div class="row-pair">
        <div><span class="lang-tag">RU — подпись ссылки</span><input class="f-popup-url-label-ru" value="${esc(popupUrlLabel.ru || '')}"></div>
        <div><span class="lang-tag">EN — link label</span><input class="f-popup-url-label-en" value="${esc(popupUrlLabel.en || '')}"></div>
      </div>
    </div>
  `;
  const actionSelect = row.querySelector('.f-action');
  const syncAction = () => {
    row.querySelector('.block-link').classList.toggle('active', actionSelect.value === 'link');
    row.querySelector('.block-popup').classList.toggle('active', actionSelect.value === 'popup');
  };
  actionSelect.addEventListener('change', syncAction);
  syncAction();

  const iconSelect = row.querySelector('.f-icon');
  const iconCode = row.querySelector('.f-icon-svg-code');
  const iconFile = row.querySelector('.f-icon-svg-file');
  const iconPreview = row.querySelector('.icon-preview');
  const iconError = row.querySelector('[data-role="icon-svg-error"]');
  const syncIcon = () => {
    const isCustom = iconSelect.value === 'custom';
    row.querySelector('.icon-custom-block').classList.toggle('active', isCustom);
    if (!isCustom) {
      iconPreview.innerHTML = '';
      iconError.textContent = '';
      return;
    }
    const code = iconCode.value.trim();
    if (!code) {
      iconPreview.innerHTML = '';
      iconError.textContent = '';
    } else if (/^<svg/i.test(code) && !/<script/i.test(code)) {
      iconPreview.innerHTML = code;
      iconError.textContent = '';
    } else {
      iconPreview.innerHTML = '';
      iconError.textContent = 'Похоже, это не SVG — код должен начинаться с <svg ...>';
    }
  };
  iconSelect.addEventListener('change', syncIcon);
  iconCode.addEventListener('input', syncIcon);
  iconFile.addEventListener('change', () => {
    const file = iconFile.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      iconCode.value = String(reader.result || '').trim();
      syncIcon();
    };
    reader.readAsText(file);
  });
  syncIcon();

  row.querySelector('.remove-link').addEventListener('click', () => row.remove());
  return row;
}

function renderLinks() {
  linksEl.innerHTML = '';
  CONTENT.about.links.forEach((link) => linksEl.appendChild(linkRow(link)));
}
renderLinks();

document.getElementById('add-link').addEventListener('click', () => {
  linksEl.appendChild(linkRow({ id: '', icon: META.knownIcons[0] || '', action: 'link', url: '', label: { ru: '', en: '' } }));
});

function collectAbout() {
  const links = [...linksEl.querySelectorAll('.card')].map((row) => {
    const action = row.querySelector('.f-action').value;
    const icon = row.querySelector('.f-icon').value;
    const base = {
      id: row.querySelector('.f-id').value.trim(),
      icon,
      action,
      label: {
        ru: row.querySelector('.f-label-ru').value.trim(),
        en: row.querySelector('.f-label-en').value.trim(),
      },
      ...(icon === 'custom' ? { iconSvg: row.querySelector('.f-icon-svg-code').value.trim() } : {}),
    };
    if (action === 'popup') {
      const popupUrl = row.querySelector('.f-popup-url').value.trim();
      return {
        ...base,
        popupTitle: {
          ru: row.querySelector('.f-popup-title-ru').value.trim(),
          en: row.querySelector('.f-popup-title-en').value.trim(),
        },
        popupText: {
          ru: row.querySelector('.f-popup-text-ru').value,
          en: row.querySelector('.f-popup-text-en').value,
        },
        ...(popupUrl
          ? {
              popupUrl,
              popupUrlLabel: {
                ru: row.querySelector('.f-popup-url-label-ru').value.trim(),
                en: row.querySelector('.f-popup-url-label-en').value.trim(),
              },
            }
          : {}),
      };
    }
    return { ...base, url: row.querySelector('.f-url').value.trim() };
  });
  const data = { links };
  for (const locale of ['ru', 'en']) {
    data[locale] = {
      description: document.getElementById(`about-${locale}-description`).value.trim(),
      license: {
        name: document.getElementById(`about-${locale}-license-name`).value.trim(),
        text: document.getElementById(`about-${locale}-license-text`).value.trim(),
      },
    };
  }
  return data;
}

document.getElementById('save-about').addEventListener('click', async () => {
  const problemEl = document.getElementById('about-problem');
  problemEl.hidden = true;
  setStatus('Сохраняем…');
  try {
    const res = await fetch('/api/about', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collectAbout()),
    });
    const body = await res.json();
    if (!res.ok || !body.ok) throw new Error(body.error || res.statusText);
    setStatus('Сохранено ✓', 'ok');
    location.reload();
  } catch (err) {
    setStatus('Ошибка', 'err');
    problemEl.hidden = false;
    problemEl.textContent = err.message;
  }
});

// ---- people (donators / bughunters / testers) ------------------------

// Mirrors _PEOPLE_SCHEMA in build_content.py - keep the two in sync by hand
// if a field is ever added, same as icons.tsx/known_icons() elsewhere is
// the one thing here that ISN'T hand-synced.
const PEOPLE_SCHEMA = {
  donators: [
    ['date', 'text', true, 'Дата (ГГГГ-ММ-ДД)'],
    ['platform', 'text', true, 'Платформа'],
    ['amount', 'text', false, 'Сумма'],
    ['comment', 'locale', false, 'Комментарий'],
  ],
  bughunters: [
    ['bugTitle', 'locale', true, 'Заголовок бага'],
    ['bugDescription', 'locale', true, 'Описание бага'],
    ['link', 'text', false, 'Ссылка на issue/коммит'],
  ],
  testers: [
    ['tested', 'locale', true, 'Что тестировал(а)'],
    ['environment', 'text', true, 'Конфигурация/ОС'],
    ['contribution', 'locale', true, 'Ключевой вклад'],
  ],
  other: [
    ['reason', 'locale', true, 'За что благодарность'],
  ],
};
const PEOPLE_CATEGORIES = Object.keys(PEOPLE_SCHEMA);

document.querySelectorAll('#people-subtabs .subtab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('#people-subtabs .subtab').forEach((t) => t.classList.remove('active'));
    document.querySelectorAll('.subpanel').forEach((p) => p.classList.remove('active'));
    tab.classList.add('active');
    document.querySelector(`.subpanel[data-sub-panel="${tab.dataset.sub}"]`).classList.add('active');
  });
});

function personCard(category, person) {
  const schema = PEOPLE_SCHEMA[category];
  const card = document.createElement('div');
  card.className = 'card';
  let fieldsHtml = '';
  for (const [field, kind, required, label] of schema) {
    if (kind === 'text') {
      fieldsHtml += `<div class="row"><label>${label}${required ? '' : ' (опц.)'}</label><input class="f-${field}" value="${esc(person[field] || '')}"></div>`;
    } else {
      const val = person[field] || {};
      fieldsHtml += `<div class="row-pair" style="margin-top:16px">
        <div><span class="lang-tag">RU — ${label}</span><textarea class="f-${field}-ru">${esc(val.ru || '')}</textarea></div>
        <div><span class="lang-tag">EN — ${label}</span><textarea class="f-${field}-en">${esc(val.en || '')}</textarea></div>
      </div>`;
    }
  }
  card.innerHTML = `
    <div class="card-head">
      <input class="f-id" placeholder="id" style="max-width:160px" value="${esc(person.id || '')}">
      <button class="ghost remove-person" type="button">Удалить</button>
    </div>
    <div class="row-pair">
      <div><span class="lang-tag">Имя / ник</span><input class="f-name" value="${esc(person.name || '')}"></div>
      <div><span class="lang-tag">Аватар — URL (опц.)</span><input class="f-avatar" value="${esc(person.avatar || '')}"></div>
    </div>
    ${fieldsHtml}
  `;
  card.querySelector('.remove-person').addEventListener('click', () => card.remove());
  return card;
}

function collectPerson(category, card) {
  const person = {
    id: card.querySelector('.f-id').value.trim(),
    name: card.querySelector('.f-name').value.trim(),
  };
  const avatar = card.querySelector('.f-avatar').value.trim();
  if (avatar) person.avatar = avatar;
  for (const [field, kind] of PEOPLE_SCHEMA[category]) {
    if (kind === 'text') {
      const value = card.querySelector(`.f-${field}`).value.trim();
      if (value) person[field] = value;
    } else {
      const ru = card.querySelector(`.f-${field}-ru`).value.trim();
      const en = card.querySelector(`.f-${field}-en`).value.trim();
      if (ru || en) person[field] = { ru, en };
    }
  }
  return person;
}

function freshId() {
  return 'p' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
}

for (const category of PEOPLE_CATEGORIES) {
  const listEl = document.getElementById(`list-${category}`);
  (CONTENT.people[category] || []).forEach((person) => listEl.appendChild(personCard(category, person)));

  document.getElementById(`add-${category}`).addEventListener('click', () => {
    listEl.appendChild(personCard(category, { id: freshId() }));
  });

  document.getElementById(`import-${category}`).addEventListener('click', () => {
    const textarea = document.getElementById(`bulk-${category}`);
    const lines = textarea.value.split('\n').map((line) => line.trim()).filter(Boolean);
    const schema = PEOPLE_SCHEMA[category];
    let added = 0;
    for (const line of lines) {
      const cols = (line.includes('\t') ? line.split('\t') : line.split(';')).map((c) => c.trim());
      if (!cols[0]) continue;
      const person = { id: freshId(), name: cols[0] };
      schema.forEach(([field, kind], i) => {
        const raw = (cols[i + 1] || '').trim();
        if (!raw) return;
        person[field] = kind === 'locale' ? { ru: raw, en: '' } : raw;
      });
      listEl.appendChild(personCard(category, person));
      added++;
    }
    textarea.value = '';
    setStatus(added ? `Импортировано строк: ${added} (не забудьте сохранить)` : 'Не найдено ни одной строки', added ? 'ok' : 'err');
  });
}

document.getElementById('save-people').addEventListener('click', async () => {
  const problemEl = document.getElementById('people-problem');
  problemEl.hidden = true;
  setStatus('Сохраняем…');
  try {
    const data = {};
    for (const category of PEOPLE_CATEGORIES) {
      data[category] = [...document.getElementById(`list-${category}`).querySelectorAll('.card')].map((card) =>
        collectPerson(category, card),
      );
    }
    const res = await fetch('/api/people', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data),
    });
    const body = await res.json();
    if (!res.ok || !body.ok) throw new Error(body.error || res.statusText);
    setStatus('Сохранено ✓', 'ok');
    location.reload();
  } catch (err) {
    setStatus('Ошибка', 'err');
    problemEl.hidden = false;
    problemEl.textContent = err.message;
  }
});

function esc(value) {
  const div = document.createElement('div');
  div.textContent = value == null ? '' : value;
  return div.innerHTML.replace(/"/g, '&quot;');
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass

    def do_GET(self) -> None:
        if self.path != "/":
            self.send_response(404)
            self.end_headers()
            return
        body = render_page().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_save(self, saver) -> None:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
            saver(data)
            result = {"ok": True}
            status = 200
        except (ValueError, json.JSONDecodeError) as exc:
            result = {"ok": False, "error": str(exc)}
            status = 400
        body = json.dumps(result).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path == "/api/about":
            self._handle_save(save_about)
        elif self.path == "/api/people":
            self._handle_save(save_people)
        else:
            self.send_response(404)
            self.end_headers()


class QuietHTTPServer(HTTPServer):
    """See edit_ui_strings.py's identical class: a browser tab closing or
    navigating away mid-response is routine, not a bug worth a traceback."""

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


def main() -> None:
    missing = [str(p) for p in (ABOUT_PATH, ICONS_PATH) if not p.exists()]
    if missing:
        raise SystemExit("Не найдены: " + ", ".join(missing))

    server = QuietHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Редактор контента открыт: {url}", flush=True)
    print("Ctrl+C здесь, чтобы остановить.", flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
