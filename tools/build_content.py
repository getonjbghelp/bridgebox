"""Local editor for BridgeBox's data-driven content: the version history
(frontend/src/data/content/changelog.json) shown from the Beta badge, and the
"about" page (frontend/src/data/content/about.json) shown on the Info screen.

Run it, a browser tab opens - the same kind of tool as edit_ui_strings.py, and
for the same reason: hand-editing two JSON files that have to stay
internally consistent (version uniqueness, RU/EN in lockstep, a link's icon
naming something that actually exists) is exactly the class of mistake a
small local editor exists to catch before it ships.

    python tools/build_content.py

stdlib only, no dependencies to install. Binds to 127.0.0.1 on an OS-chosen
free port so it never collides with anything else running locally.
"""

from __future__ import annotations

import json
import re
import sys
import webbrowser
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
CONTENT_DIR = FRONTEND_SRC / "data" / "content"
CHANGELOG_PATH = CONTENT_DIR / "changelog.json"
ABOUT_PATH = CONTENT_DIR / "about.json"
ICONS_PATH = FRONTEND_SRC / "components" / "icons.tsx"
LOGO_PATH = FRONTEND_SRC / "components" / "BrandLogo.tsx"
PYPROJECT_PATH = REPO_ROOT / "backend" / "pyproject.toml"

LOCALES = ("ru", "en")
LEVELS = ("minor", "major", "critical")

_PYPROJECT_VERSION_RE = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.MULTILINE)
# Same two patterns backend/bridgebox/version.py derives the Beta badge's
# label with. Vendored rather than imported: this script has to run with
# nothing but the system's stdlib Python, not the backend's venv, and a
# regex this small is cheaper to keep in sync by hand than to add an
# import path workaround for.
_PRERELEASE_RE = re.compile(r"(?:a|b|rc)\d+$")
_VERSION_PART = re.compile(r"\d+")
_VIEWBOX_RE = re.compile(r'viewBox="([\d.\s-]+)"')
_PATH_D_RE = re.compile(r'd="(M[^"]+)"')
_LINK_ICONS_RE = re.compile(r"export const LINK_ICONS = \{(.*?)\n\}", re.DOTALL)
_ICON_KEY_RE = re.compile(r"(\w+):\s*Icon\w+")


def suggested_version() -> str:
    """The label a NEW changelog entry should probably use - "b1" from
    pyproject's "0.1.0b1", same derivation as version.release_label(). Not
    forced: a hotfix or backport might legitimately want a different one, so
    the page shows this as a starting point, not a locked field."""
    match = _PYPROJECT_VERSION_RE.search(PYPROJECT_PATH.read_text(encoding="utf-8"))
    version_string = match.group(1) if match else ""
    pre = _PRERELEASE_RE.search(version_string)
    if pre:
        return pre.group(0)
    parts = _VERSION_PART.findall(version_string)
    return ".".join(parts[:2]) if len(parts) >= 2 else version_string


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
    changelog = json.loads(CHANGELOG_PATH.read_text(encoding="utf-8"))
    about = json.loads(ABOUT_PATH.read_text(encoding="utf-8"))
    return {"changelog": changelog, "about": about}


def build_meta() -> dict:
    return {
        "suggestedVersion": suggested_version(),
        "today": date.today().isoformat(),
        "knownIcons": known_icons(),
        "levels": list(LEVELS),
    }


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


def _is_valid_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme == "mailto":
        return bool(parsed.path)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def validate_changelog(entries: object) -> list[dict]:
    if not isinstance(entries, list):
        raise ValueError("история версий должна быть списком")

    seen_versions: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"запись {i + 1}: должна быть объектом")
        version = entry.get("version")
        if not isinstance(version, str) or not version.strip():
            raise ValueError(f"запись {i + 1}: версия не может быть пустой")
        if version in seen_versions:
            raise ValueError(f"версия {version!r} повторяется")
        seen_versions.add(version)

        entry_date = entry.get("date")
        if not isinstance(entry_date, str):
            raise ValueError(f"{version}: дата обязательна")
        try:
            date.fromisoformat(entry_date)
        except ValueError:
            raise ValueError(f"{version}: дата {entry_date!r} не в формате ГГГГ-ММ-ДД") from None

        if entry.get("level") not in LEVELS:
            raise ValueError(f"{version}: level должен быть одним из {', '.join(LEVELS)}")

        for locale in LOCALES:
            text = entry.get(locale)
            if not isinstance(text, dict):
                raise ValueError(f"{version}: раздел {locale!r} обязателен")
            if not isinstance(text.get("title"), str) or not text["title"].strip():
                raise ValueError(f"{version} ({locale}): заголовок не может быть пустым")
            if not isinstance(text.get("body"), str) or not text["body"].strip():
                raise ValueError(f"{version} ({locale}): тело записи не может быть пустым")
    return entries


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


def save_changelog(entries: object) -> None:
    validated = validate_changelog(entries)
    _atomic_write(CHANGELOG_PATH, validated)


def save_about(data: object) -> None:
    validated = validate_about(data)
    _atomic_write(ABOUT_PATH, validated)


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
    <button class="tab active" data-panel="changelog">История версий</button>
    <button class="tab" data-panel="about">О программе</button>
  </div>
  <span id="status"></span>
</header>
<main>
  <section id="panel-changelog" class="panel active">
    <p class="hint">Новые версии добавляются сверху. Версия по умолчанию — <code id="suggested-version"></code>, взято из pyproject.toml; при желании можно вписать другую (например, для хотфикса).</p>
    <button class="primary" id="add-entry">Добавить версию</button>
    <div id="entries"></div>
    <button class="primary save-bar" id="save-changelog">Сохранить историю версий</button>
    <p class="problem" id="changelog-problem" hidden></p>
  </section>

  <section id="panel-about" class="panel">
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
</main>
<script>
const CONTENT = __CONTENT_JSON__;
const META = __META_JSON__;
document.getElementById('suggested-version').textContent = META.suggestedVersion;

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

// ---- changelog ----------------------------------------------------------

const entriesEl = document.getElementById('entries');

function entryCard(entry, index) {
  const card = document.createElement('div');
  card.className = 'card';
  card.dataset.index = index;
  card.innerHTML = `
    <div class="card-head">
      <span class="badge badge--${entry.level}">${entry.level}</span>
      <button class="ghost remove-entry" type="button">Удалить</button>
    </div>
    <div class="row"><label>Версия</label><input class="f-version" value="${esc(entry.version)}"></div>
    <div class="row"><label>Дата</label><input class="f-date" type="date" value="${esc(entry.date)}"></div>
    <div class="row"><label>Важность</label>
      <select class="f-level">
        ${META.levels.map((l) => `<option value="${l}"${l === entry.level ? ' selected' : ''}>${l}</option>`).join('')}
      </select>
    </div>
    <div class="row-pair">
      <div>
        <span class="lang-tag">RU — заголовок</span><input class="f-ru-title" value="${esc(entry.ru.title)}">
        <span class="lang-tag" style="margin-top:14px">RU — текст (- список, **жирный**, \`код\`)</span>
        <textarea class="tall f-ru-body">${esc(entry.ru.body)}</textarea>
      </div>
      <div>
        <span class="lang-tag">EN — title</span><input class="f-en-title" value="${esc(entry.en.title)}">
        <span class="lang-tag" style="margin-top:14px">EN — body</span>
        <textarea class="tall f-en-body">${esc(entry.en.body)}</textarea>
      </div>
    </div>
  `;
  card.querySelector('.f-level').addEventListener('change', (e) => {
    card.querySelector('.badge').className = 'badge badge--' + e.target.value;
  });
  card.querySelector('.remove-entry').addEventListener('click', () => {
    if (confirm('Удалить запись ' + entry.version + '?')) card.remove();
  });
  return card;
}

function renderEntries() {
  entriesEl.innerHTML = '';
  CONTENT.changelog.forEach((entry, i) => entriesEl.appendChild(entryCard(entry, i)));
}
renderEntries();

document.getElementById('add-entry').addEventListener('click', () => {
  const fresh = {
    version: META.suggestedVersion,
    date: META.today,
    level: 'minor',
    ru: { title: '', body: '' },
    en: { title: '', body: '' },
  };
  entriesEl.insertBefore(entryCard(fresh, 0), entriesEl.firstChild);
});

function collectChangelog() {
  return [...entriesEl.querySelectorAll('.card')].map((card) => ({
    version: card.querySelector('.f-version').value.trim(),
    date: card.querySelector('.f-date').value,
    level: card.querySelector('.f-level').value,
    ru: {
      title: card.querySelector('.f-ru-title').value.trim(),
      body: card.querySelector('.f-ru-body').value,
    },
    en: {
      title: card.querySelector('.f-en-title').value.trim(),
      body: card.querySelector('.f-en-body').value,
    },
  }));
}

document.getElementById('save-changelog').addEventListener('click', async () => {
  const problemEl = document.getElementById('changelog-problem');
  problemEl.hidden = true;
  setStatus('Сохраняем…');
  try {
    const res = await fetch('/api/changelog', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collectChangelog()),
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
        if self.path == "/api/changelog":
            self._handle_save(save_changelog)
        elif self.path == "/api/about":
            self._handle_save(save_about)
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
    missing = [str(p) for p in (CHANGELOG_PATH, ABOUT_PATH, ICONS_PATH) if not p.exists()]
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
