"""Local editor for BridgeBox's UI text (frontend/src/data/strings/{ru,en}.json).

Run it, a browser tab opens with every piece of UI copy grouped by screen,
Russian and English side by side. Edit any field, hit "Сохранить" (or Ctrl+S) -
it writes straight back to both files. After saving, rebuild the frontend to
see it in the app:

    cd frontend && npm run build       # or `npm run dev` for live preview

Each field is labelled with what is special about it - which {placeholders}
the app interpolates, whether backticks in it become monospace, and whether
the UI still reads the string at all. Those labels are read out of the
frontend source on every page load rather than listed here (scan_source),
so the editor cannot fall behind a feature being added or removed - the
failure mode a hand-maintained list would have. The logo in the header is
lifted out of BrandLogo.tsx for the same reason.

Both locale files must carry exactly the same keys - the app picks a catalog
by locale at runtime (see lib/strings.ts's `satisfies` check), and a key
missing from one would only fail for whoever has that language selected.
save_strings enforces this, and the placeholders inside a key ({count}, etc.)
have to match between languages too, for the same reason.

A string the UI no longer references can also be deleted from here, from
either language - it always removes both, since a key existing in only one
locale is exactly the state this tool refuses to save. Deleting a string the
UI *does* read is refused by save_strings, not merely discouraged in the page.

Usage:
    python tools/edit_ui_strings.py

stdlib only, no dependencies to install. Binds to 127.0.0.1 on an OS-chosen
free port so it never collides with anything else running locally.
"""

from __future__ import annotations

import json
import re
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
STRINGS_DIR = FRONTEND_SRC / "data" / "strings"
LOCALES = ("ru", "en")
STRINGS_PATHS = {locale: STRINGS_DIR / f"{locale}.json" for locale in LOCALES}
LOGO_PATH = FRONTEND_SRC / "components" / "BrandLogo.tsx"

_REF_RE = re.compile(r"strings\.(\w+)\.(\w+)")
_RICH_RE = re.compile(r"renderRich\(\s*strings\.(\w+)\.(\w+)\s*\)")
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
_VIEWBOX_RE = re.compile(r'viewBox="([\d.\s-]+)"')
_PATH_D_RE = re.compile(r'd="(M[^"]+)"')


def read_wordmark() -> str:
    """The app's own logo, lifted out of the component that draws it.

    Read rather than copied for the same reason scan_source exists: a second
    copy of the artwork is a copy that goes stale the next time the logo
    changes, and this tool is supposed to look like the app it edits. Returns
    an empty string if the component moves or its shape changes, in which case
    the header simply falls back to its text title."""
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


def load_strings() -> dict[str, dict]:
    return {locale: json.loads(path.read_text(encoding="utf-8")) for locale, path in STRINGS_PATHS.items()}


def scan_source() -> tuple[set[str], set[str]]:
    """Read the two facts the editor needs straight out of the frontend
    source, as {"section.key"} sets: which strings the UI still references,
    and which are rendered through renderRich (so backticks in them become
    <code> instead of literal backticks).

    Derived rather than hardcoded on purpose. A list maintained here would
    silently rot every time a feature is added or removed - which is exactly
    how this tool would end up describing an app that no longer exists."""
    source = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in FRONTEND_SRC.rglob("*.ts*")
    )
    referenced = {f"{s}.{k}" for s, k in _REF_RE.findall(source)}
    rich = {f"{s}.{k}" for s, k in _RICH_RE.findall(source)}
    return referenced, rich


def build_meta(data: dict[str, dict]) -> dict:
    """Per-field rules shown next to each pair of inputs. Required
    placeholders come from the Russian value currently on disk - whatever
    {name} it interpolates today is what an edit, in either language, must
    keep."""
    referenced, rich = scan_source()
    placeholders = {
        f"{section}.{key}": _PLACEHOLDER_RE.findall(value)
        for section, fields in data["ru"].items()
        for key, value in fields.items()
        if _PLACEHOLDER_RE.search(value)
    }
    return {
        "referenced": sorted(referenced),
        "rich": sorted(rich),
        "placeholders": placeholders,
    }


def save_strings(payload: dict[str, dict]) -> None:
    if not isinstance(payload, dict) or set(payload) != set(LOCALES):
        raise ValueError(f"нужен объект с ключами {', '.join(LOCALES)}")

    for locale, data in payload.items():
        if not isinstance(data, dict):
            raise ValueError(f"{locale}: верхний уровень должен быть объектом")
        for section, fields in data.items():
            if not isinstance(fields, dict):
                raise ValueError(f"{locale}: раздел {section!r} должен быть объектом")
            for key, value in fields.items():
                if not isinstance(value, str):
                    raise ValueError(f"{locale}: {section}.{key} должен быть текстом")

    # Every key must exist in every language - lib/strings.ts's `satisfies`
    # check enforces this at compile time for whatever ships, but this tool
    # can produce a state that has not been rebuilt yet, so it checks too.
    key_sets = {
        locale: {f"{section}.{key}" for section, fields in data.items() for key in fields}
        for locale, data in payload.items()
    }
    base_locale, *rest = LOCALES
    for locale in rest:
        only_here = key_sets[locale] - key_sets[base_locale]
        only_base = key_sets[base_locale] - key_sets[locale]
        if only_here:
            raise ValueError(f"{locale} содержит лишние ключи: {', '.join(sorted(only_here))}")
        if only_base:
            raise ValueError(f"в {locale} не хватает ключей: {', '.join(sorted(only_base))}")

    # {placeholders} have to match across languages too - the app fills them
    # in by name regardless of which catalog answered, so a translation that
    # drops or renames one leaves a literal "{count}" on screen for whoever
    # has that language selected.
    mismatched = []
    for section, fields in payload[base_locale].items():
        for key, base_value in fields.items():
            base_ph = set(_PLACEHOLDER_RE.findall(base_value))
            for locale in rest:
                other_ph = set(_PLACEHOLDER_RE.findall(payload[locale][section][key]))
                if other_ph != base_ph:
                    mismatched.append(f"{section}.{key} ({base_locale} vs {locale})")
    if mismatched:
        raise ValueError("плейсхолдеры разошлись между языками: " + ", ".join(mismatched))

    # The page can delete keys, so this is the last thing standing between a
    # stray click and an app rendering "undefined" where a label should be.
    # Checked here rather than only in the browser: the page is one client of
    # this endpoint, and the files it writes are what the build compiles in.
    referenced, _ = scan_source()
    present = key_sets[base_locale]
    missing = sorted(referenced - present)
    if missing:
        raise ValueError(
            "интерфейс читает эти строки, без них он сломается: " + ", ".join(missing)
        )

    # Write beside the target and rename over it, so a crash mid-write (or a
    # full disk) never leaves one file half-written while the other already
    # changed - each locale's write is atomic on its own, and there is
    # nothing left to roll back across the two since both were validated
    # above before either write starts.
    for locale, data in payload.items():
        path = STRINGS_PATHS[locale]
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)


def render_page() -> str:
    data = load_strings()
    return (
        PAGE_TEMPLATE.replace("__STRINGS_JSON__", json.dumps(data, ensure_ascii=False))
        .replace("__META_JSON__", json.dumps(build_meta(data), ensure_ascii=False))
        .replace("__WORDMARK__", read_wordmark())
    )


PAGE_TEMPLATE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>BridgeBox — редактор текста</title>
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

  /* The app it edits has a dark theme; following the OS here costs one block
     and stops this tool being the one white rectangle on a dark desktop. */
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
    font: 14px/1.5 -apple-system, "Segoe UI", system-ui, sans-serif;
  }
  header {
    position: sticky; top: 0; z-index: 10;
    display: flex; align-items: center; justify-content: space-between;
    gap: 16px; padding: 16px 32px; background: var(--surface);
    border-bottom: 1px solid var(--border);
  }
  h1 { font-size: 18px; margin: 0; white-space: nowrap; }
  .brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
  /* currentColor, so the wordmark follows the theme the same way it does in
     the app - it is the same artwork, read out of BrandLogo.tsx. */
  .logo { height: 22px; width: auto; fill: currentColor; color: var(--text); display: block; }
  main { max-width: 1080px; margin: 0 auto; padding: 24px 32px 96px; }
  input#search {
    flex: 1; max-width: 360px; padding: 8px 12px; font: inherit;
    border: 1px solid var(--border-strong); border-radius: 8px; background: var(--sunken);
  }
  button {
    font: inherit; font-weight: 600; padding: 8px 18px; border-radius: 8px;
    border: 1px solid transparent; cursor: pointer; background: var(--accent); color: #fff;
  }
  button:hover { opacity: 0.9; }
  button.ghost {
    background: transparent; color: var(--text-3); border-color: var(--border-strong);
    font-weight: 500; padding: 3px 10px; font-size: 12px;
  }
  button.ghost:hover { color: var(--danger); border-color: var(--danger); opacity: 1; }
  #status { font-size: 13px; color: var(--text-3); min-width: 120px; text-align: right; }
  #status.ok { color: var(--success); }
  #status.err { color: var(--danger); }
  section.group { margin-bottom: 32px; }
  section.group h2 {
    font-size: 15px; margin: 0 0 12px; padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }
  .field { padding: 10px 0; border-bottom: 1px solid var(--border); }
  .field:last-child { border-bottom: none; }
  .field.hidden { display: none; }
  .field label {
    display: block; font-size: 12px; color: var(--text-3);
    margin-bottom: 4px; font-family: "Cascadia Mono", Consolas, monospace;
  }
  .field-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
  .field-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .field-lang { display: flex; flex-direction: column; gap: 2px; }
  .field-lang-tag {
    font-size: 10px; font-weight: 700; letter-spacing: 0.04em; color: var(--text-3);
  }
  .field textarea {
    width: 100%; resize: vertical; min-height: 38px; padding: 8px 10px;
    font: inherit; border: 1px solid var(--border-strong); border-radius: 8px;
    background: var(--surface); color: var(--text);
  }
  .field textarea:focus { outline: 2px solid var(--accent); outline-offset: 1px; }
  .field textarea.dirty { border-color: var(--accent); background: var(--accent-soft); }
  .field textarea.bad { border-color: var(--danger); background: var(--danger-soft); }
  /* Marked for removal: still on screen and still reversible until saved. */
  .field.doomed { opacity: 0.55; }
  .field.doomed textarea { text-decoration: line-through; border-style: dashed; }
  .badges { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 4px; }
  .badge {
    font-size: 11px; padding: 1px 7px; border-radius: 999px;
    font-family: "Cascadia Mono", Consolas, monospace;
  }
  .badge--ph { background: var(--accent-soft); color: var(--accent); }
  .badge--rich { background: var(--success-soft); color: var(--success); }
  .badge--unused { background: var(--warn-soft); color: var(--warn); }
  .problem { margin: 4px 0 0; font-size: 12px; color: var(--danger); }
  .hint {
    max-width: 1080px; margin: 0 auto; padding: 12px 32px 0; color: var(--text-3); font-size: 13px;
  }
  code { font-family: "Cascadia Mono", Consolas, monospace; background: var(--sunken); padding: 1px 5px; border-radius: 4px; }
</style>
</head>
<body>
<header>
  <div class="brand">__WORDMARK__<h1>Текст интерфейса — RU / EN</h1></div>
  <input id="search" type="text" placeholder="Найти текст или подпись поля…">
  <span id="status"></span>
  <button id="save">Сохранить</button>
</header>
<p class="hint">
  Метки над полем говорят, что в нём особенного:
  <span class="badge badge--ph">{author}</span> — приложение подставит сюда значение, эту вставку нельзя убирать, переименовывать или терять при переводе;
  <span class="badge badge--rich">`код`</span> — только в этом поле обратные кавычки превращаются в моноширинный шрифт, в остальных они останутся обычными кавычками;
  <span class="badge badge--unused">не используется</span> — строка осталась в файлах, но интерфейс её больше нигде не показывает; такую можно удалить кнопкой рядом (сразу из обоих языков).
  После сохранения пересоберите фронтенд (<code>npm run build</code> в <code>frontend/</code>), чтобы увидеть изменения в приложении.
</p>
<main id="app"></main>
<script>
const DATA = __STRINGS_JSON__;
// Read out of the frontend source at page load - see scan_source().
const META = __META_JSON__;
const LOCALES = ['ru', 'en'];
const SECTION_TITLES = {
  common: "Общее (используется на нескольких экранах)",
  sidebar: "Боковая панель",
  home: "Экран «Запуск»",
  settings: "Экран «Настройки»",
  setup: "Мастер первоначальной настройки",
  logs: "Экран «Логи»",
};

const app = document.getElementById('app');
const dirty = new Set();
// Keys the user has marked for removal. Only ever keys META says nothing
// references - the button does not exist on the others - and nothing leaves
// the files until Сохранить, so it stays reversible until then.
const doomed = new Set();

/** What an edit broke, compared to the value the app is running today. */
function problemsFor(id, value) {
  const problems = [];
  const required = META.placeholders[id] || [];
  const present = [...value.matchAll(/\\{(\\w+)\\}/g)].map((m) => m[1]);

  const missing = required.filter((name) => !present.includes(name));
  if (missing.length) {
    problems.push('пропала подстановка ' + missing.map((n) => '{' + n + '}').join(', ') +
      ' — на её месте в приложении будет пусто');
  }
  const unknown = present.filter((name) => !required.includes(name));
  if (unknown.length) {
    problems.push('приложение не знает ' + unknown.map((n) => '{' + n + '}').join(', ') +
      ' — эти скобки покажутся пользователю как есть');
  }
  if (META.rich.includes(id) && (value.split('`').length - 1) % 2 !== 0) {
    problems.push('непарная обратная кавычка — часть текста не станет моноширинной');
  }
  return problems;
}

function badge(text, kind, title) {
  const el = document.createElement('span');
  el.className = 'badge badge--' + kind;
  el.textContent = text;
  if (title) el.title = title;
  return el;
}

for (const section of Object.keys(DATA.ru)) {
  const groupEl = document.createElement('section');
  groupEl.className = 'group';
  groupEl.dataset.section = section;

  const h2 = document.createElement('h2');
  h2.textContent = SECTION_TITLES[section] || section;
  groupEl.appendChild(h2);

  for (const key of Object.keys(DATA.ru[section])) {
    const id = section + '.' + key;
    const fieldEl = document.createElement('div');
    fieldEl.className = 'field';
    const searchText = LOCALES.map((l) => DATA[l][section][key]).join(' ').toLowerCase();
    fieldEl.dataset.searchText = (key + ' ' + searchText).toLowerCase();

    const head = document.createElement('div');
    head.className = 'field-head';
    const label = document.createElement('label');
    label.textContent = key;
    head.appendChild(label);

    // Only offered for strings the UI no longer reads. A refactor leaves these
    // behind by the dozen, and removing one by hand means editing two JSON
    // files - which is the exact thing this tool exists to avoid.
    if (!META.referenced.includes(id)) {
      const del = document.createElement('button');
      del.type = 'button';
      del.className = 'ghost';
      del.textContent = 'Удалить';
      del.title = 'Убрать эту строку из обоих языков при сохранении';
      del.addEventListener('click', () => {
        const now = !doomed.has(id);
        if (now) doomed.add(id); else doomed.delete(id);
        fieldEl.classList.toggle('doomed', now);
        del.textContent = now ? 'Вернуть' : 'Удалить';
        fieldEl.querySelectorAll('textarea').forEach((ta) => (ta.disabled = now));
        setStatus('Есть несохранённые изменения', '');
      });
      head.appendChild(del);
    }

    const badges = document.createElement('div');
    badges.className = 'badges';
    (META.placeholders[id] || []).forEach((name) => {
      badges.appendChild(badge('{' + name + '}', 'ph', 'Приложение подставит сюда значение - должно быть в обоих языках'));
    });
    if (META.rich.includes(id)) {
      badges.appendChild(badge('`код`', 'rich', 'Текст в обратных кавычках станет моноширинным'));
    }
    if (!META.referenced.includes(id)) {
      badges.appendChild(badge('не используется', 'unused',
        'Интерфейс нигде не читает эту строку - её правка ни на что не повлияет'));
    }

    const problemEl = document.createElement('p');
    problemEl.className = 'problem';
    problemEl.hidden = true;

    function refreshProblems() {
      const problems = LOCALES.flatMap((l) => problemsFor(id, DATA[l][section][key]));
      problemEl.hidden = problems.length === 0;
      problemEl.textContent = [...new Set(problems)].join('; ');
    }

    const pair = document.createElement('div');
    pair.className = 'field-pair';
    for (const locale of LOCALES) {
      const langCol = document.createElement('div');
      langCol.className = 'field-lang';
      const tag = document.createElement('span');
      tag.className = 'field-lang-tag';
      tag.textContent = locale.toUpperCase();
      const textarea = document.createElement('textarea');
      textarea.id = id + '.' + locale;
      textarea.value = DATA[locale][section][key];
      textarea.rows = textarea.value.length > 70 ? 3 : 1;
      textarea.addEventListener('input', () => {
        DATA[locale][section][key] = textarea.value;
        textarea.classList.add('dirty');
        dirty.add(id);
        const problems = problemsFor(id, textarea.value);
        textarea.classList.toggle('bad', problems.length > 0);
        refreshProblems();
        setStatus('Есть несохранённые изменения', '');
      });
      langCol.appendChild(tag);
      langCol.appendChild(textarea);
      pair.appendChild(langCol);
    }

    fieldEl.appendChild(head);
    if (badges.children.length) fieldEl.appendChild(badges);
    fieldEl.appendChild(pair);
    fieldEl.appendChild(problemEl);
    groupEl.appendChild(fieldEl);
  }
  app.appendChild(groupEl);
}

document.getElementById('search').addEventListener('input', (e) => {
  const q = e.target.value.trim().toLowerCase();
  document.querySelectorAll('.field').forEach((el) => {
    el.classList.toggle('hidden', q !== '' && !el.dataset.searchText.includes(q));
  });
  document.querySelectorAll('.group').forEach((g) => {
    const anyVisible = [...g.querySelectorAll('.field')].some((f) => !f.classList.contains('hidden'));
    g.style.display = anyVisible ? '' : 'none';
  });
});

function setStatus(text, cls) {
  const el = document.getElementById('status');
  el.textContent = text;
  el.className = cls;
}

async function save() {
  const payload = JSON.parse(JSON.stringify(DATA));
  const broken = [];
  for (const section of Object.keys(payload.ru)) {
    for (const key of Object.keys(payload.ru[section])) {
      const id = section + '.' + key;
      if (doomed.has(id)) {
        for (const locale of LOCALES) delete payload[locale][section][key];
        continue;
      }
      for (const locale of LOCALES) {
        const problems = problemsFor(id, payload[locale][section][key]);
        if (problems.length) broken.push(locale + ' ' + key + ': ' + problems.join('; '));
      }
    }
  }

  // Deletions are confirmed separately from the placeholder warnings below:
  // one is "this text may render oddly", the other removes lines from both
  // files, and rolling them into one prompt would let a stray Enter do both.
  if (doomed.size && !confirm(
    'Будут удалены из обоих языков (' + doomed.size + '):\\n\\n' +
    [...doomed].join('\\n') + '\\n\\nПродолжить?'
  )) {
    setStatus('Сохранение отменено', '');
    return;
  }

  // Warn rather than block - it is the user's file, and a placeholder they
  // genuinely want gone should not need a text editor to remove.
  if (broken.length && !confirm(
    'В этих строках что-то не так:\\n\\n' + broken.join('\\n') + '\\n\\nВсё равно сохранить?'
  )) {
    setStatus('Сохранение отменено', '');
    return;
  }

  setStatus('Сохраняем…', '');
  try {
    const res = await fetch('/api/strings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const body = await res.json();
    if (!res.ok || !body.ok) throw new Error(body.error || res.statusText);
    document.querySelectorAll('textarea.dirty').forEach((ta) => ta.classList.remove('dirty'));
    dirty.clear();
    if (doomed.size) {
      // The page was built from files that no longer exist in that shape;
      // reloading is cheaper and more honest than patching the DOM.
      setStatus('Сохранено ✓', 'ok');
      location.reload();
      return;
    }
    setStatus('Сохранено ✓', 'ok');
  } catch (err) {
    setStatus('Ошибка: ' + err.message, 'err');
  }
}

document.getElementById('save').addEventListener('click', save);
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); save(); }
});
window.addEventListener('beforeunload', (e) => {
  if (dirty.size > 0 || doomed.size > 0) { e.preventDefault(); e.returnValue = ''; }
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa: A002 - stdlib signature
        pass  # Quiet by default; the browser tab is the UI, not this console.

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

    def do_POST(self) -> None:
        if self.path != "/api/strings":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
            save_strings(data)
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


class QuietHTTPServer(HTTPServer):
    """socketserver's default handle_error prints a full traceback to
    stderr for *any* unhandled exception mid-request - including the
    routine case of the browser tab closing or navigating away while a
    response is still being written (ConnectionAbortedError on Windows,
    BrokenPipeError/ConnectionResetError elsewhere). That is not a bug in
    this tool; printing it as one is exactly the kind of scary output that
    makes a non-programmer distrust a script that is actually fine."""

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, BrokenPipeError, ConnectionResetError)):
            return  # the browser walked away mid-response - nothing to report
        super().handle_error(request, client_address)


def main() -> None:
    missing = [str(path) for path in STRINGS_PATHS.values() if not path.exists()]
    if missing:
        raise SystemExit("Не найдены: " + ", ".join(missing))

    server = QuietHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Редактор текста открыт: {url}", flush=True)
    print("Ctrl+C здесь, чтобы остановить.", flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
