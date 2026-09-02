#!/usr/bin/env python3
"""
Tkinter UI для sync_and_publish.py

Удобный GUI-вариант:
- синхронизация через robocopy;
- просмотр изменений по папкам;
- коммит выбранных папок;
- общий коммит;
- push.
"""

from __future__ import annotations

import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import webbrowser
import zipfile
from collections import defaultdict
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from tkinter.scrolledtext import ScrolledText

# Every gh/release call targets this repo explicitly, rather than whatever
# the public_dir clone's own git remote happens to be named or point at.
REPO_SLUG = "getonjbghelp/bridgebox"

# WinRAR ships no reliable way to add itself to PATH - these are the two
# locations its own installer actually uses. .rar creation gives up with a
# clear message if neither exists, rather than guessing further.
WINRAR_CANDIDATES = [
    Path(r"C:\Program Files\WinRAR\Rar.exe"),
    Path(r"C:\Program Files (x86)\WinRAR\Rar.exe"),
]


EXCLUDE_DIRS = [
    "example"
]

EXCLUDE_FILES = [
    "example"
]

# How long "Прикрепить к релизу" waits for GitHub Actions to have actually
# published the release before giving up. CI's own build (PyInstaller on a
# fresh windows-latest runner) routinely takes several minutes, and this is
# what lets the button be clicked right after "Опубликовать релиз" instead
# of making the user watch Actions and come back later.
ATTACH_WAIT_POLL_S = 15.0
ATTACH_WAIT_TIMEOUT_S = 20 * 60
WAIT_HEARTBEAT_S = 120.0


def decode_git_quoted_path(path: str) -> str:
    """
    git status может выдавать пути в кавычках и с экранированными байтами,
    например: "файл \\320\\274.txt".
    Эта функция пытается нормально распаковать такой путь.
    """
    path = path.strip()

    if not (path.startswith('"') and path.endswith('"')):
        return path

    body = path[1:-1]
    raw = bytearray()
    i = 0

    simple_escapes = {
        "\\": 0x5C,
        '"': 0x22,
        "a": 7,
        "b": 8,
        "f": 12,
        "n": 10,
        "r": 13,
        "t": 9,
        "v": 11,
    }

    while i < len(body):
        ch = body[i]

        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]

            # Восьмеричные экранированные байты: \320\274 ...
            if nxt in "01234567":
                oct_digits = ""
                j = i + 1
                while j < len(body) and len(oct_digits) < 3 and body[j] in "01234567":
                    oct_digits += body[j]
                    j += 1

                try:
                    raw.append(int(oct_digits, 8))
                except ValueError:
                    raw.extend(nxt.encode("utf-8", "ignore"))

                i = j
                continue

            if nxt in simple_escapes:
                raw.append(simple_escapes[nxt])
                i += 2
                continue

            raw.extend(nxt.encode("utf-8", "ignore"))
            i += 2
            continue

        raw.extend(ch.encode("utf-8", "ignore"))
        i += 1

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace")


def rename_parts(entry: str) -> tuple[str, str] | None:
    """
    Если entry - запись вида "old -> new" (как их кладёт parse_git_status
    ниже для переименований), вернуть (old, new). Иначе None.
    """
    if " -> " not in entry:
        return None
    old, new = entry.rsplit(" -> ", 1)
    return old, new


def parse_git_status(output: str) -> dict[str, list[str]]:
    """
    Разбирает вывод `git status --porcelain` и группирует файлы по первой папке
    (папке НОВОГО пути для переименований). Файлы в корне публичного репозитория
    попадают в группу '.'.

    Запись переименования хранится целиком как "old -> new", а не только новый
    путь - иначе точечный коммит по папке (`git add -A -- <new>`) стейджит
    появление нового файла, но не видит путь старого вообще, и удаление
    старого файла остаётся вне pathspec-а - особенно заметно, если старый и
    новый путь лежат в разных папках. rename_parts() выше распаковывает эту
    запись там, где она реально стейджится или показывается.
    """
    groups: dict[str, list[str]] = defaultdict(list)

    for line in output.splitlines():
        if not line.strip():
            continue

        parts = line.split(maxsplit=1)
        if len(parts) < 2:
            continue

        rest = parts[1]

        old_filepath = None
        if " -> " in rest:
            old_part, rest = rest.rsplit(" -> ", 1)
            old_filepath = decode_git_quoted_path(old_part)

        filepath = decode_git_quoted_path(rest)
        if not filepath:
            continue

        # git может показывать неотслеживаемую папку как "some_dir/"
        is_dir_entry = filepath.endswith("/")
        filepath = filepath.rstrip("/")

        p = Path(filepath)

        if is_dir_entry and p.parts:
            folder = p.parts[0]
        elif p.parent == Path("."):
            folder = "."
        else:
            folder = p.parent.parts[0]

        entry = f"{old_filepath} -> {filepath}" if old_filepath else filepath
        groups[folder].append(entry)

    return dict(groups)


def guess_repo_root() -> Path:
    """
    Пытается угадать корень проекта.
    Обычно файл лежит в tools/, тогда корень — родительская папка tools/.
    """
    try:
        here = Path(__file__).resolve()
    except NameError:
        here = Path.cwd() / "sync_and_publish_ui.py"

    candidates: list[Path] = []

    if here.parent.name.lower() == "tools":
        candidates.append(here.parent.parent)

    candidates.extend([here.parent, *here.parents, Path.cwd()])

    seen: set[Path] = set()

    for candidate in candidates:
        try:
            candidate = candidate.resolve()
        except OSError:
            continue

        if candidate in seen:
            continue

        seen.add(candidate)

        if (candidate / "githubpubliccode").is_dir():
            return candidate

    if here.parent.name.lower() == "tools":
        return here.parent.parent

    return Path.cwd()


_VERSION_RE = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.MULTILINE)
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_TITLE_SPLIT_RE = re.compile(r"^(.*?)([a-zA-Z]+\d+)$")


def _guess_release_title(version: str) -> str:
    """"0.1.9b1" -> "0.1.9 (b1)" - matches the Name every past release on
    GitHub actually has, distinct from its bare tag_name. Only a starting
    point for the "Название релиза" field, not authoritative: a tag with no
    trailing <letters><digits> prerelease suffix (or an unusual one) comes
    back unchanged for the user to fill in by hand."""
    version = version.strip()
    match = _TITLE_SPLIT_RE.match(version)
    if not match or not match.group(1):
        return version
    base, suffix = match.groups()
    return f"{base} ({suffix})"


def _insert_inline_markdown(widget: tk.Text, line: str, base_tags: tuple[str, ...]) -> None:
    """**bold** within one line; everything else is plain text. Not a real
    markdown parser - just the one inline construct these release notes
    actually use, so the preview needs no new dependency to render it."""
    pos = 0
    for m in _MD_BOLD_RE.finditer(line):
        if m.start() > pos:
            widget.insert(tk.END, line[pos : m.start()], base_tags)
        widget.insert(tk.END, m.group(1), base_tags + ("bold",))
        pos = m.end()
    if pos < len(line):
        widget.insert(tk.END, line[pos:], base_tags)


def render_markdown_preview(widget: tk.Text, markdown_text: str) -> None:
    """Renders the block-level subset of markdown a GitHub release actually
    needs (headers, bullet lists, paragraphs, **bold**) into `widget` via Tk
    text tags - a real preview of how GitHub will show it, without pulling in
    a markdown library or an embedded browser for a one-screen internal tool."""
    widget.configure(state="normal")
    widget.delete("1.0", tk.END)

    widget.tag_configure("h1", font=("Segoe UI", 15, "bold"), spacing3=6)
    widget.tag_configure("h2", font=("Segoe UI", 12, "bold"), spacing3=4)
    widget.tag_configure("body", font=("Segoe UI", 10))
    widget.tag_configure("bullet", font=("Segoe UI", 10), lmargin1=18, lmargin2=30)
    widget.tag_configure("bold", font=("Segoe UI", 10, "bold"))

    for raw_line in markdown_text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("### "):
            widget.insert(tk.END, line[4:] + "\n", "h2")
        elif line.startswith("## "):
            widget.insert(tk.END, line[3:] + "\n", "h2")
        elif line.startswith("# "):
            widget.insert(tk.END, line[2:] + "\n", "h1")
        elif line.startswith("- ") or line.startswith("* "):
            _insert_inline_markdown(widget, "• " + line[2:] + "\n", ("bullet",))
        elif not line:
            widget.insert(tk.END, "\n")
        else:
            _insert_inline_markdown(widget, line + "\n", ("body",))

    widget.configure(state="disabled")


class SyncPublishUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Sync & Publish UI")
        self.root.geometry("1280x820")
        self.root.minsize(1080, 700)

        self.q: queue.Queue = queue.Queue()
        self.groups: dict[str, list[str]] = {}
        self.item_to_folder: dict[str, str] = {}
        self.busy = False
        self.first_groups = True

        default_root = guess_repo_root()

        self.repo_var = tk.StringVar(value=str(default_root))
        self.public_var = tk.StringVar(value=str(default_root / "githubpubliccode"))
        self.msg_var = tk.StringVar(value="Sync before new release - N")
        self.ask_per_folder_var = tk.BooleanVar(value=False)
        self.release_version_var = tk.StringVar(value=self._read_repo_version())
        # Guessed once at startup, not re-derived when the version above is
        # refreshed - so a title the user already typed by hand is never
        # silently clobbered by a later "Обновить из pyproject.toml" click.
        self.release_title_var = tk.StringVar(
            value=_guess_release_title(self.release_version_var.get())
        )
        # Files to attach to an ALREADY-published GitHub release (see
        # on_attach_files) - e.g. a .rar built locally, since release.yml's
        # Compress-Archive only ever produces a .zip.
        self.attach_files: list[Path] = []
        self.gh_status_var = tk.StringVar(value="gh: проверяем...")
        self.latest_release_var = tk.StringVar(value="")

        self._build_ui()
        self._poll_queue()
        self.refresh_changes()
        # Unprompted, not tied to any button - so a missing/unauthenticated
        # gh shows up as a status label before the user clicks Publish/
        # Attach/rar and only then discovers it from a wall of log text.
        threading.Thread(target=self._check_gh_status, daemon=True).start()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        # Пути - stays above the tabs, not inside any one of them: every
        # tab's operations depend on these two paths, so which tree is
        # active should never be hidden behind a tab click.
        paths = ttk.LabelFrame(main, text="Пути", padding=8)
        paths.pack(fill="x")
        paths.columnconfigure(1, weight=1)

        ttk.Label(paths, text="Dev tree (источник):").grid(
            row=0, column=0, sticky="w", padx=(0, 8)
        )
        ttk.Entry(paths, textvariable=self.repo_var).grid(
            row=0, column=1, sticky="ew"
        )
        ttk.Button(paths, text="Обзор...", width=10, command=self._browse_repo).grid(
            row=0, column=2, padx=(8, 0)
        )

        ttk.Label(paths, text="Public repo:").grid(
            row=1, column=0, sticky="w", padx=(0, 8), pady=(6, 0)
        )
        ttk.Entry(paths, textvariable=self.public_var).grid(
            row=1, column=1, sticky="ew", pady=(6, 0)
        )
        ttk.Button(paths, text="Обзор...", width=10, command=self._browse_public).grid(
            row=1, column=2, padx=(8, 0), pady=(6, 0)
        )

        # Everything below used to be one long vertical stack - sync
        # controls, commit, local build, and the whole release form all
        # visible (or not, past the fold) at once. Three tabs instead, one
        # per actual phase of the workflow; the log and status bar stay
        # outside the notebook (below) since every tab's actions write to
        # the same one and it should never need a tab switch to check.
        notebook = ttk.Notebook(main)
        notebook.pack(fill="both", expand=True, pady=(8, 0))

        sync_tab = ttk.Frame(notebook, padding=8)
        build_tab = ttk.Frame(notebook, padding=8)
        release_tab = ttk.Frame(notebook, padding=8)
        notebook.add(sync_tab, text="Синхронизация")
        notebook.add(build_tab, text="Сборка")
        notebook.add(release_tab, text="Релиз")

        # ---- Синхронизация -------------------------------------------

        toolbar1 = ttk.Frame(sync_tab)
        toolbar1.pack(fill="x")

        self.btn_sync = ttk.Button(
            toolbar1, text="Синхронизировать", command=self.on_sync
        )
        self.btn_refresh = ttk.Button(
            toolbar1, text="Обновить статус", command=self.refresh_changes
        )
        self.btn_push = ttk.Button(toolbar1, text="Push", command=self.on_push)
        self.btn_select_all = ttk.Button(
            toolbar1, text="Выбрать все", command=self._select_all_folders
        )
        self.btn_clear_selection = ttk.Button(
            toolbar1, text="Снять выбор", command=self._clear_selection
        )

        for btn in (
            self.btn_sync,
            self.btn_refresh,
            self.btn_push,
            self.btn_select_all,
            self.btn_clear_selection,
        ):
            btn.pack(side="left", padx=(0, 6))

        toolbar2 = ttk.Frame(sync_tab)
        toolbar2.pack(fill="x", pady=(4, 0))

        self.btn_commit_selected = ttk.Button(
            toolbar2,
            text="Коммит выбранных папок",
            command=self.on_commit_selected,
        )
        self.btn_commit_all = ttk.Button(
            toolbar2,
            text="Коммит все изменения",
            command=self.on_commit_all,
        )

        for btn in (self.btn_commit_selected, self.btn_commit_all):
            btn.pack(side="left", padx=(0, 6))

        # Сообщение коммита
        commit_frame = ttk.LabelFrame(sync_tab, text="Коммит", padding=6)
        commit_frame.pack(fill="x", pady=(8, 0))
        commit_frame.columnconfigure(1, weight=1)

        ttk.Label(commit_frame, text="Сообщение коммита:").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Entry(commit_frame, textvariable=self.msg_var).grid(
            row=0, column=1, sticky="ew", padx=(8, 0)
        )

        ttk.Checkbutton(
            commit_frame,
            text="Спрашивать сообщение для каждой папки отдельно",
            variable=self.ask_per_folder_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # Область изменений
        split = ttk.Panedwindow(sync_tab, orient="horizontal")
        split.pack(fill="both", expand=True, pady=(8, 0))

        left = ttk.Frame(split)
        right = ttk.Frame(split)
        split.add(left, weight=1)
        split.add(right, weight=2)

        # Список папок
        ttk.Label(left, text="Папки с изменениями:").pack(anchor="w")

        folder_frame = ttk.Frame(left)
        folder_frame.pack(fill="both", expand=True)

        self.folder_tree = ttk.Treeview(
            folder_frame,
            columns=("folder", "count"),
            show="headings",
            selectmode="extended",
        )
        self.folder_tree.heading("#1", text="Папка")
        self.folder_tree.heading("#2", text="Файлов")
        self.folder_tree.column("#1", width=280)
        self.folder_tree.column("#2", width=80, anchor="center")

        folder_scroll = ttk.Scrollbar(
            folder_frame, orient="vertical", command=self.folder_tree.yview
        )
        self.folder_tree.configure(yscrollcommand=folder_scroll.set)

        self.folder_tree.pack(side="left", fill="both", expand=True)
        folder_scroll.pack(side="right", fill="y")

        self.folder_tree.bind("<<TreeviewSelect>>", self._on_folder_select)

        # Список файлов
        ttk.Label(right, text="Файлы:").pack(anchor="w")

        file_frame = ttk.Frame(right)
        file_frame.pack(fill="both", expand=True)

        self.file_tree = ttk.Treeview(
            file_frame,
            columns=("folder", "file"),
            show="headings",
            selectmode="browse",
        )
        self.file_tree.heading("#1", text="Папка")
        self.file_tree.heading("#2", text="Файл")
        self.file_tree.column("#1", width=180)
        self.file_tree.column("#2", width=520)

        file_scroll = ttk.Scrollbar(
            file_frame, orient="vertical", command=self.file_tree.yview
        )
        self.file_tree.configure(yscrollcommand=file_scroll.set)

        self.file_tree.pack(side="left", fill="both", expand=True)
        file_scroll.pack(side="right", fill="y")

        # ---- Сборка -----------------------------------------------------
        # Локальная сборка - runs tools/build_portable.py against public_dir
        # (the public checkout - what anyone building from a source clone
        # actually gets). Deliberately the PUBLIC build script, not
        # build_portable_internal.py: this one is what carries the
        # "Community rebuild - verify the source before trusting this copy"
        # PE-resource comment and always forces a fresh frontend build
        # (VITE_BB_BUILD_KIND=src) - exactly what should mark any build that
        # runs from this, the public-facing tool. See
        # sync_and_publish_internal.py's own version of this button for the
        # maintainer-only half (repo_dir, no marker, --skip-frontend-build
        # allowed).
        ttk.Label(
            build_tab,
            text="Собирает bridgebox.exe локально (frontend + PyInstaller) и упаковывает "
            "в .zip и .rar внутри dist\\ - способ убедиться, что релиз реально собирается, "
            "не дожидаясь CI.",
        ).pack(anchor="w")
        self.btn_build_release = ttk.Button(
            build_tab, text="Собрать релиз", command=self.on_build_release
        )
        self.btn_build_release.pack(anchor="w", pady=(6, 0))

        # ---- Релиз --------------------------------------------------
        # Публикация релиза - тегирует public_dir's HEAD (после push) с
        # текстом ниже как сообщением тега; .github/workflows/release.yml
        # читает этот тег и делает всё остальное (сборка, зип, GitHub Release).
        # A separate pack()'d bar, not more grid rows inside release_frame -
        # this is orientation (which repo, is gh even usable, a shortcut
        # into the browser), not part of the publish/attach/rar forms below
        # it, and keeping it out of that grid means adding to it never
        # forces every row number in release_frame to shift.
        release_info = ttk.Frame(release_tab)
        release_info.pack(fill="x")
        ttk.Label(release_info, text=f"Репозиторий: {REPO_SLUG}", foreground="#666").pack(
            side="left"
        )
        ttk.Label(release_info, textvariable=self.gh_status_var, foreground="#666").pack(
            side="left", padx=(16, 0)
        )
        ttk.Label(release_info, textvariable=self.latest_release_var, foreground="#666").pack(
            side="left", padx=(16, 0)
        )
        ttk.Button(
            release_info, text="Открыть на GitHub", command=self.open_release_on_github
        ).pack(side="right")

        release_frame = ttk.LabelFrame(release_tab, text="Публикация релиза", padding=6)
        release_frame.pack(fill="x", pady=(8, 0))
        release_frame.columnconfigure(1, weight=1)

        ttk.Label(release_frame, text="Версия (тег):").grid(row=0, column=0, sticky="w")
        ttk.Entry(release_frame, textvariable=self.release_version_var, width=20).grid(
            row=0, column=1, sticky="w", padx=(8, 0)
        )
        ttk.Button(
            release_frame,
            text="Обновить из pyproject.toml",
            command=self._refresh_release_version,
        ).grid(row=0, column=2, padx=(8, 0), sticky="w")

        # Title, distinct from the tag - every past release has one ("0.1.9
        # (b1)" as its Name, vs "0.1.9b1" as its tag_name), but nothing
        # transported it: release.yml used to fall back to the bare tag as
        # --title. Guessed once from the version at startup only - refreshing
        # the version above never touches a title the user may have already
        # customised.
        ttk.Label(release_frame, text="Название релиза (заголовок):").grid(
            row=1, column=0, sticky="w", pady=(4, 0)
        )
        ttk.Entry(release_frame, textvariable=self.release_title_var).grid(
            row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(4, 0)
        )

        ttk.Label(release_frame, text="Описание релиза (Markdown, RU+EN):").grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )
        self.release_notes_text = ScrolledText(release_frame, height=8, wrap="word")
        self.release_notes_text.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(2, 0))

        release_buttons = ttk.Frame(release_frame)
        release_buttons.grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Button(
            release_buttons, text="Просмотр (Markdown)", command=self._show_release_preview
        ).pack(side="left", padx=(0, 6))
        self.btn_publish_release = ttk.Button(
            release_buttons, text="Опубликовать релиз", command=self.on_publish_release
        )
        self.btn_publish_release.pack(side="left")

        # Прикрепление доп. файлов к релизу на GitHub - для всего, что CI не
        # производит (например .rar - Compress-Archive в release.yml делает
        # только .zip). Отдельная кнопка от "Опубликовать релиз", но не
        # обязательно отдельный МОМЕНТ: можно выбрать файлы и нажать сразу
        # после публикации тега - _worker_attach_files сам подождёт, пока
        # GitHub Actions действительно соберёт и опубликует релиз, прежде
        # чем пытаться что-то к нему приложить (см. её докстринг).
        ttk.Label(
            release_frame,
            text="Прикрепить файлы к релизу (например, .rar - CI собирает только .zip). "
            "Можно выбрать сразу после публикации - подождём сборку CI:",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 0))

        attach_frame = ttk.Frame(release_frame)
        attach_frame.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(2, 0))
        attach_frame.columnconfigure(0, weight=1)

        self.attach_listbox = tk.Listbox(attach_frame, height=3, selectmode="extended")
        self.attach_listbox.grid(row=0, column=0, sticky="ew")
        attach_scroll = ttk.Scrollbar(
            attach_frame, orient="vertical", command=self.attach_listbox.yview
        )
        self.attach_listbox.configure(yscrollcommand=attach_scroll.set)
        attach_scroll.grid(row=0, column=1, sticky="ns")

        attach_buttons = ttk.Frame(release_frame)
        attach_buttons.grid(row=7, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Button(
            attach_buttons, text="Добавить файлы...", command=self._add_attach_files
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            attach_buttons, text="Убрать выбранное", command=self._remove_attach_files
        ).pack(side="left", padx=(0, 6))
        ttk.Button(
            attach_buttons, text="Очистить", command=self._clear_attach_files
        ).pack(side="left", padx=(0, 6))
        self.btn_attach_files = ttk.Button(
            attach_buttons, text="Прикрепить к релизу", command=self.on_attach_files
        )
        self.btn_attach_files.pack(side="left", padx=(0, 6))
        # Не берёт файлы из списка выше - сам скачивает .zip, который уже
        # опубликовал CI, распаковывает и пересобирает в .rar тем же
        # WinRAR, что стоит на этой машине. Гарантирует, что .rar - те же
        # байты, что реально ушли в релиз, а не отдельная локальная сборка.
        self.btn_create_rar = ttk.Button(
            attach_buttons, text="Создать .rar из релиза", command=self.on_create_rar
        )
        self.btn_create_rar.pack(side="left", padx=(0, 6))
        ttk.Label(
            release_frame,
            text="Требует установленный и авторизованный gh CLI (gh auth login); "
            "«Создать .rar» также требует локальный WinRAR.",
            foreground="#666",
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(2, 0))

        # Лог - outside the notebook on purpose (see the comment above it),
        # fixed to its own height=10 rather than expand=True so it never
        # eats space the notebook's own tabs need to stay readable.
        log_frame = ttk.Frame(main)
        log_frame.pack(fill="x", pady=(8, 0))

        ttk.Label(log_frame, text="Лог:").pack(anchor="w")
        self.log = ScrolledText(log_frame, height=10, state="disabled", wrap="none")
        self.log.pack(fill="both", expand=True)

        # Статус
        self.status_var = tk.StringVar(value="Готово")
        ttk.Label(
            main,
            textvariable=self.status_var,
            relief="sunken",
            anchor="w",
            padding=(4, 2),
        ).pack(fill="x", pady=(8, 0))

        self.buttons = [
            self.btn_sync,
            self.btn_refresh,
            self.btn_push,
            self.btn_select_all,
            self.btn_clear_selection,
            self.btn_commit_selected,
            self.btn_commit_all,
            self.btn_publish_release,
            self.btn_attach_files,
            self.btn_create_rar,
            self.btn_build_release,
        ]

    # ------------------------------------------------------------------
    # Basic helpers
    # ------------------------------------------------------------------

    @property
    def repo_dir(self) -> Path:
        return Path(self.repo_var.get().strip() or ".")

    @property
    def public_dir(self) -> Path:
        return Path(self.public_var.get().strip() or ".")

    def _read_repo_version(self) -> str:
        """Current version from the DEV tree's own pyproject.toml, not
        public_dir's copy - that one only catches up after the next sync,
        so reading it here could show a version that was already superseded."""
        pyproject = self.repo_dir / "backend" / "pyproject.toml"
        try:
            text = pyproject.read_text(encoding="utf-8")
        except OSError:
            return ""
        match = _VERSION_RE.search(text)
        return match.group(1) if match else ""

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert(tk.END, str(text).rstrip() + "\n")
        self.log.see(tk.END)
        self.log.configure(state="disabled")

    def _set_busy(self, busy: bool, status: str | None = None) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"

        for btn in self.buttons:
            btn.configure(state=state)

        if status:
            self.status_var.set(status)

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()

                if kind == "log":
                    self._append_log(payload)

                elif kind == "groups":
                    self._update_groups(payload)

                elif kind == "done":
                    self._set_busy(False, payload or "Готово")

                elif kind == "error":
                    self._set_busy(False, "Ошибка")
                    messagebox.showerror(
                        "Sync & Publish UI",
                        str(payload),
                        parent=self.root,
                    )

                elif kind == "gh_status":
                    self.gh_status_var.set(payload)

                elif kind == "latest_release":
                    self.latest_release_var.set(payload)

                elif kind == "built_version":
                    self.release_version_var.set(payload)
        except queue.Empty:
            pass

        self.root.after(100, self._poll_queue)

    def _start_thread(self, target, *args, status: str = "Выполняется...") -> None:
        if self.busy:
            return

        self._set_busy(True, status)
        threading.Thread(
            target=self._thread_guard,
            args=(target, *args),
            daemon=True,
        ).start()

    def _thread_guard(self, target, *args) -> None:
        try:
            target(*args)
        except Exception as exc:
            self.q.put(("log", f"Ошибка: {exc}"))
            self.q.put(("log", traceback.format_exc()))
            self.q.put(("done", "Ошибка"))

    def _ensure_public_repo(self) -> bool:
        public = self.public_dir

        if not public.is_dir():
            messagebox.showerror(
                "Папка не найдена",
                f"Папка не найдена:\n{public}",
                parent=self.root,
            )
            return False

        if not (public / ".git").exists():
            messagebox.showerror(
                "Не git-репозиторий",
                f"Папка не похожа на git-репозиторий:\n{public}\n\n"
                "Ожидается наличие .git внутри.",
                parent=self.root,
            )
            return False

        return True

    def _check_gh_status(self) -> None:
        """Runs off the main thread like every other subprocess call here,
        but bypasses _run_process/self.q's "log" channel on purpose - this
        is a passive one-line status, not an action the user asked for, and
        dumping `gh auth status`'s own verbose output into the log pane on
        every launch would just be noise."""
        try:
            proc = subprocess.run(
                ["gh", "auth", "status"], capture_output=True, text=True, errors="replace",
            )
        except FileNotFoundError:
            self.q.put(("gh_status", "gh: не установлен"))
            return
        except Exception as exc:
            self.q.put(("gh_status", f"gh: ошибка проверки ({exc})"))
            return

        if proc.returncode != 0:
            self.q.put(("gh_status", "gh: установлен, но не авторизован (gh auth login)"))
            return

        match = re.search(r"account (\S+)", proc.stdout + proc.stderr)
        who = match.group(1) if match else "?"
        self.q.put(("gh_status", f"gh: ✓ авторизован как {who}"))

        # Same reason as the auth check: this is "what's actually live right
        # now", worth knowing before touching Версия/Название/Опубликовать,
        # not something to make the user go find on GitHub first.
        try:
            latest = subprocess.run(
                [
                    "gh", "release", "view", "--repo", REPO_SLUG,
                    "--json", "tagName", "--jq", ".tagName",
                ],
                capture_output=True, text=True, errors="replace",
            )
        except Exception:
            return
        if latest.returncode == 0 and latest.stdout.strip():
            self.q.put(("latest_release", f"последний на GitHub: {latest.stdout.strip()}"))

    # ------------------------------------------------------------------
    # External commands
    # ------------------------------------------------------------------

    def _run_process(
        self,
        cmd: list[str],
        cwd: str | None = None,
        log_cmd: bool = True,
        log_output: bool = True,
    ) -> subprocess.CompletedProcess:
        if log_cmd:
            self.q.put(("log", "$ " + " ".join(str(x) for x in cmd)))

        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                errors="replace",
            )
        except FileNotFoundError as exc:
            self.q.put(("log", f"Команда не найдена: {cmd[0]}"))
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=127,
                stdout="",
                stderr=str(exc),
            )
        except Exception as exc:
            self.q.put(("log", f"Ошибка запуска команды: {exc}"))
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr=str(exc),
            )

        if proc.stdout and log_output:
            self.q.put(("log", proc.stdout.rstrip()))

        if proc.stderr and log_output:
            self.q.put(("log", proc.stderr.rstrip()))

        return proc

    def _run_git(
        self,
        args: list[str],
        log_cmd: bool = True,
        log_output: bool = True,
        log_errors: bool = True,
    ) -> subprocess.CompletedProcess:
        public = self.public_dir

        if not public.is_dir():
            msg = f"Public dir не существует: {public}"
            self.q.put(("log", msg))
            return subprocess.CompletedProcess(
                args=["git", *args],
                returncode=1,
                stdout="",
                stderr=msg,
            )

        proc = self._run_process(
            ["git", *args],
            cwd=str(public),
            log_cmd=log_cmd,
            log_output=log_output,
        )

        # Если вывод ошибки не включён, но команда упала — покажем ошибку отдельно.
        if proc.returncode != 0 and log_errors and proc.stderr and not log_output:
            self.q.put(("log", proc.stderr.rstrip()))

        return proc

    def _collect_changes(self) -> dict[str, list[str]]:
        public = self.public_dir

        if not public.is_dir():
            return {}

        if not (public / ".git").exists():
            self.q.put(("log", f"Папка не является git-репозиторием: {public}"))
            return {}

        proc = self._run_git(
            ["status", "--porcelain"],
            log_cmd=False,
            log_output=False,
            log_errors=True,
        )

        if proc.returncode != 0:
            return {}

        return parse_git_status(proc.stdout or "")

    # ------------------------------------------------------------------
    # UI actions
    # ------------------------------------------------------------------

    def _browse_repo(self) -> None:
        initial = self.repo_dir if self.repo_dir.is_dir() else Path.cwd()

        selected = filedialog.askdirectory(
            parent=self.root,
            initialdir=str(initial),
            title="Выберите папку проекта",
        )

        if not selected:
            return

        self.repo_var.set(selected)

        candidate_public = Path(selected) / "githubpubliccode"
        current_public = self.public_dir

        # Если выбран проект с типичной папкой назначения — подставить её.
        if candidate_public.is_dir() and (
            not current_public.is_dir() or current_public.name == "githubpubliccode"
        ):
            self.public_var.set(str(candidate_public))

    def _browse_public(self) -> None:
        initial = self.public_dir if self.public_dir.is_dir() else Path.cwd()

        selected = filedialog.askdirectory(
            parent=self.root,
            initialdir=str(initial),
            title="Выберите папку публичного репозитория",
        )

        if selected:
            self.public_var.set(selected)

    def _refresh_release_version(self) -> None:
        version = self._read_repo_version()
        if version:
            self.release_version_var.set(version)
        else:
            messagebox.showerror(
                "Публикация релиза",
                f"Не удалось прочитать версию из {self.repo_dir / 'backend' / 'pyproject.toml'}",
                parent=self.root,
            )

    def _show_release_preview(self) -> None:
        text = self.release_notes_text.get("1.0", tk.END)
        if not text.strip():
            messagebox.showinfo(
                "Предпросмотр", "Текст описания релиза пуст.", parent=self.root
            )
            return

        top = tk.Toplevel(self.root)
        top.title("Предпросмотр (Markdown)")
        top.geometry("640x520")
        preview = ScrolledText(top, wrap="word")
        preview.pack(fill="both", expand=True, padx=8, pady=8)
        render_markdown_preview(preview, text)

    def _refresh_attach_listbox(self) -> None:
        self.attach_listbox.delete(0, tk.END)
        for path in self.attach_files:
            self.attach_listbox.insert(tk.END, path.name)

    def _add_attach_files(self) -> None:
        selected = filedialog.askopenfilenames(
            parent=self.root,
            title="Выберите файлы для прикрепления к релизу",
        )
        existing = {p.resolve() for p in self.attach_files}
        for raw in selected:
            path = Path(raw)
            if path.resolve() not in existing:
                self.attach_files.append(path)
                existing.add(path.resolve())
        self._refresh_attach_listbox()

    def _remove_attach_files(self) -> None:
        selected_indices = set(self.attach_listbox.curselection())
        self.attach_files = [
            p for i, p in enumerate(self.attach_files) if i not in selected_indices
        ]
        self._refresh_attach_listbox()

    def _clear_attach_files(self) -> None:
        self.attach_files = []
        self._refresh_attach_listbox()

    def _select_all_folders(self) -> None:
        children = self.folder_tree.get_children()
        self.folder_tree.selection_set(children)
        self._show_selected_files()

    def _clear_selection(self) -> None:
        self.folder_tree.selection_remove(self.folder_tree.get_children())
        self._show_selected_files()

    def _selected_folders(self) -> list[str]:
        result: list[str] = []

        for iid in self.folder_tree.selection():
            folder = self.item_to_folder.get(iid)
            if folder is not None:
                result.append(folder)

        return result

    def _on_folder_select(self, event=None) -> None:
        self._show_selected_files()

    def _show_selected_files(self) -> None:
        self.file_tree.delete(*self.file_tree.get_children())

        for iid in self.folder_tree.selection():
            folder = self.item_to_folder.get(iid)
            if not folder:
                continue

            display_folder = "(корень)" if folder == "." else folder

            for filepath in self.groups.get(folder, []):
                self.file_tree.insert(
                    "",
                    "end",
                    values=(display_folder, filepath),
                )

    def _update_groups(self, groups: dict[str, list[str]]) -> None:
        old_selected = self._selected_folders()

        self.groups = groups or {}
        self.folder_tree.delete(*self.folder_tree.get_children())
        self.file_tree.delete(*self.file_tree.get_children())
        self.item_to_folder.clear()

        sorted_folders = sorted(
            self.groups.keys(),
            key=lambda x: (x != ".", x.lower()),
        )

        for folder in sorted_folders:
            files = self.groups[folder]
            display_folder = "(корень)" if folder == "." else folder

            iid = self.folder_tree.insert(
                "",
                "end",
                values=(display_folder, len(files)),
            )

            self.item_to_folder[iid] = folder

        to_select: list[str] = []

        for iid, folder in self.item_to_folder.items():
            if self.first_groups:
                to_select.append(iid)
            elif old_selected and folder in old_selected:
                to_select.append(iid)

        if self.first_groups:
            self.folder_tree.selection_set(to_select)
            self.first_groups = False
        elif old_selected:
            self.folder_tree.selection_set(to_select)

        self._show_selected_files()

        total_files = sum(len(files) for files in self.groups.values())
        total_folders = len(self.groups)

        if total_files == 0:
            self.status_var.set("Изменений нет.")
        else:
            self.status_var.set(
                f"Изменений: {total_files} файлов в {total_folders} папках."
            )

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------

    def on_sync(self) -> None:
        if self.busy:
            return

        if sys.platform != "win32":
            if not messagebox.askyesno(
                "Внимание",
                "Этот скрипт использует robocopy, который обычно доступен только в Windows.\n"
                "Продолжить?",
                parent=self.root,
            ):
                return

        if not self.repo_dir.is_dir():
            messagebox.showerror(
                "Папка не найдена",
                f"Исходная папка не найдена:\n{self.repo_dir}",
                parent=self.root,
            )
            return

        public = self.public_dir

        if not public.is_dir():
            if messagebox.askyesno(
                "Папка не найдена",
                f"Папка не найдена:\n{public}\n\nСоздать её?",
                parent=self.root,
            ):
                try:
                    public.mkdir(parents=True, exist_ok=True)
                    self._append_log(f"Создана папка: {public}")
                except Exception as exc:
                    messagebox.showerror(
                        "Ошибка",
                        f"Не удалось создать папку:\n{exc}",
                        parent=self.root,
                    )
                    return
            else:
                return

        if not (public / ".git").exists():
            if not messagebox.askyesno(
                "Внимание",
                f"Папка назначения не является git-репозиторием:\n{public}\n\n"
                "Продолжить?",
                parent=self.root,
            ):
                return

        # После синхронизации удобно заново выделить все изменения.
        self.first_groups = True

        self._start_thread(
            self._worker_sync,
            status="Синхронизация...",
        )

    def refresh_changes(self) -> None:
        if self.busy:
            return

        if not self.public_dir.is_dir():
            self._update_groups({})
            self.status_var.set("Папка публичного репозитория не найдена.")
            return

        self._start_thread(
            self._worker_refresh,
            status="Обновление статуса...",
        )

    def on_commit_selected(self) -> None:
        if self.busy:
            return

        if not self._ensure_public_repo():
            return

        selected = self._selected_folders()

        if not selected:
            messagebox.showinfo(
                "Нет выбора",
                "Выберите одну или несколько папок слева.",
                parent=self.root,
            )
            return

        default_msg = self.msg_var.get().strip() or "Sync before new release - N"

        tasks: list[tuple[str, list[str], str]] = []

        for folder in selected:
            files = self.groups.get(folder, [])
            if not files:
                continue

            if folder == ".":
                msg = default_msg
            else:
                msg = f"{default_msg} ({folder})"

            if self.ask_per_folder_var.get():
                entered = simpledialog.askstring(
                    parent=self.root,
                    title="Сообщение коммита",
                    prompt=f"Папка: {folder}\nФайлов: {len(files)}\nСообщение коммита:",
                    initialvalue=msg,
                )

                # Отмена пользователя.
                if entered is None:
                    return

                msg = entered.strip() or msg

            tasks.append((folder, files, msg))

        if not tasks:
            messagebox.showinfo(
                "Нет изменений",
                "Для выбранных папок нет файлов для коммита.",
                parent=self.root,
            )
            return

        if not messagebox.askyesno(
            "Подтверждение",
            f"Закоммитить выбранные папки?\n"
            f"Папок: {len(tasks)}\n"
            "Для каждой папки будет создан отдельный коммит.",
            parent=self.root,
        ):
            return

        self._start_thread(
            self._worker_commit_folders,
            tasks,
            status="Коммит...",
        )

    def on_commit_all(self) -> None:
        if self.busy:
            return

        if not self._ensure_public_repo():
            return

        if not self.groups:
            if not messagebox.askyesno(
                "Нет изменений",
                "Список изменений пуст.\nВсё равно выполнить общий коммит?",
                parent=self.root,
            ):
                return

        msg = self.msg_var.get().strip()

        if not msg:
            entered = simpledialog.askstring(
                parent=self.root,
                title="Сообщение коммита",
                prompt="Сообщение для общего коммита:",
                initialvalue="Sync before new release - N",
            )

            if entered is None:
                return

            msg = entered.strip()

            if not msg:
                messagebox.showwarning(
                    "Пустое сообщение",
                    "Сообщение коммита не может быть пустым.",
                    parent=self.root,
                )
                return

        if not messagebox.askyesno(
            "Подтверждение",
            "Закоммитить все изменения одним общим коммитом?",
            parent=self.root,
        ):
            return

        self._start_thread(
            self._worker_commit_all,
            msg,
            status="Коммит...",
        )

    def on_push(self) -> None:
        if self.busy:
            return

        if not self._ensure_public_repo():
            return

        if not messagebox.askyesno(
            "Push",
            "Выполнить git push?",
            parent=self.root,
        ):
            return

        self._start_thread(
            self._worker_push,
            status="Push...",
        )

    def on_publish_release(self) -> None:
        if self.busy:
            return

        if not self._ensure_public_repo():
            return

        version = self.release_version_var.get().strip()
        if not version:
            messagebox.showerror("Публикация релиза", "Укажите версию.", parent=self.root)
            return

        title = self.release_title_var.get().strip()
        if not title:
            messagebox.showerror(
                "Публикация релиза", "Укажите название релиза.", parent=self.root
            )
            return

        notes = self.release_notes_text.get("1.0", tk.END).strip()
        if not notes:
            messagebox.showerror(
                "Публикация релиза", "Текст описания релиза пуст.", parent=self.root
            )
            return

        # Fail fast, before the confirm dialog: _worker_publish_release
        # checks this too (the authoritative guard against a race with
        # something else tagging in between), but finding out only after
        # clicking "Да" - from a log line, mid-"Публикация релиза..." - is
        # a worse way to learn "you already published this version".
        existing = self._run_git(
            ["rev-parse", "-q", "--verify", f"refs/tags/{version}"],
            log_cmd=False, log_output=False, log_errors=False,
        )
        if existing.returncode == 0:
            messagebox.showerror(
                "Публикация релиза",
                f"Тег «{version}» уже существует в public-репозитории.",
                parent=self.root,
            )
            return

        # What will actually get tagged - a public_dir on the wrong branch,
        # or with unpushed local commits nobody meant to ship, is exactly
        # the kind of mistake a confirm dialog should make hard to miss
        # rather than silently tag whatever HEAD happens to be.
        branch_proc = self._run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"], log_cmd=False, log_output=False, log_errors=False,
        )
        branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 else "?"
        commit_proc = self._run_git(
            ["log", "-1", "--format=%h %s"], log_cmd=False, log_output=False, log_errors=False,
        )
        commit_line = commit_proc.stdout.strip() if commit_proc.returncode == 0 else "?"

        if not messagebox.askyesno(
            "Публикация релиза",
            f"Создать и запушить тег «{version}» с названием «{title}»?\n\n"
            f"Ветка: {branch}\nКоммит: {commit_line}\n\n"
            "GitHub Actions соберёт портативный релиз и опубликует его на "
            "GitHub сразу, без черновика.",
            parent=self.root,
        ):
            return

        self._start_thread(
            self._worker_publish_release,
            version,
            title,
            notes,
            status="Публикация релиза...",
        )

    def on_attach_files(self) -> None:
        if self.busy:
            return

        if not self._ensure_public_repo():
            return

        version = self.release_version_var.get().strip()
        if not version:
            messagebox.showerror(
                "Прикрепление файлов", "Укажите версию (тег) релиза.", parent=self.root
            )
            return

        if not self.attach_files:
            messagebox.showinfo(
                "Прикрепление файлов", "Список файлов пуст.", parent=self.root
            )
            return

        missing = [p for p in self.attach_files if not p.is_file()]
        if missing:
            messagebox.showerror(
                "Прикрепление файлов",
                "Файл(ы) не найдены:\n" + "\n".join(str(p) for p in missing),
                parent=self.root,
            )
            return

        names = "\n".join(p.name for p in self.attach_files)
        if not messagebox.askyesno(
            "Прикрепление файлов",
            f"Прикрепить к релизу «{version}» на GitHub:\n{names}\n\n"
            "Уже существующий ассет с тем же именем будет заменён.",
            parent=self.root,
        ):
            return

        self._start_thread(
            self._worker_attach_files,
            version,
            list(self.attach_files),
            status="Прикрепление файлов...",
        )

    def on_create_rar(self) -> None:
        if self.busy:
            return

        version = self.release_version_var.get().strip()
        if not version:
            messagebox.showerror(
                "Создание .rar", "Укажите версию (тег) релиза.", parent=self.root
            )
            return

        if self._find_winrar() is None:
            messagebox.showerror(
                "Создание .rar",
                "WinRAR не найден. Ожидался один из путей:\n"
                + "\n".join(str(p) for p in WINRAR_CANDIDATES),
                parent=self.root,
            )
            return

        if not messagebox.askyesno(
            "Создание .rar",
            f"Скачать .zip релиза «{version}», пересобрать в .rar тем же WinRAR "
            "и прикрепить его к тому же релизу на GitHub?",
            parent=self.root,
        ):
            return

        self._start_thread(
            self._worker_create_rar,
            version,
            status="Создание .rar...",
        )

    def open_release_on_github(self) -> None:
        version = self.release_version_var.get().strip()
        url = (
            f"https://github.com/{REPO_SLUG}/releases/tag/{version}"
            if version
            else f"https://github.com/{REPO_SLUG}/releases"
        )
        webbrowser.open(url)

    def on_build_release(self) -> None:
        if self.busy:
            return

        tree = self.public_dir
        script = tree / "tools" / "build_portable.py"
        if not script.is_file():
            messagebox.showerror(
                "Сборка релиза",
                f"Не найден {script} - синхронизируйте public-репозиторий сначала.",
                parent=self.root,
            )
            return

        python_exe = tree / "backend" / ".venv" / "Scripts" / "python.exe"
        if not python_exe.is_file():
            messagebox.showerror(
                "Сборка релиза",
                f"Не найден {python_exe} - выполните run.bat в public-репозитории "
                "один раз, чтобы создать venv.",
                parent=self.root,
            )
            return

        if not messagebox.askyesno(
            "Сборка релиза",
            f"Собрать портативный релиз из {tree} (frontend + PyInstaller + .zip + .rar)?\n\n"
            "Это может занять несколько минут.",
            parent=self.root,
        ):
            return

        self._start_thread(self._worker_build_release, status="Сборка релиза...")

    # ------------------------------------------------------------------
    # Background workers
    # ------------------------------------------------------------------

    def _worker_sync(self) -> None:
        repo = self.repo_dir
        public = self.public_dir

        cmd = [
            "robocopy",
            str(repo),
            str(public),
            "/E",
            "/XD",
            *EXCLUDE_DIRS,
            "/XF",
            *EXCLUDE_FILES,
        ]

        proc = self._run_process(cmd, log_output=True)

        if proc.returncode >= 8:
            self.q.put(
                (
                    "log",
                    f"robocopy завершился с ошибкой (код {proc.returncode}).",
                )
            )
            self.q.put(("done", f"robocopy failed: {proc.returncode}"))
            return

        self.q.put(
            (
                "log",
                f"robocopy exit code: {proc.returncode} (0-7 считается нормой).",
            )
        )

        groups = self._collect_changes()
        self.q.put(("groups", groups))
        self.q.put(("done", "Синхронизация завершена."))

    def _worker_refresh(self) -> None:
        groups = self._collect_changes()
        self.q.put(("groups", groups))
        self.q.put(("done", "Статус обновлён."))

    def _worker_commit_folders(
        self,
        tasks: list[tuple[str, list[str], str]],
    ) -> None:
        for folder, files, msg in tasks:
            self.q.put(
                (
                    "log",
                    f"--- Коммит для папки '{folder}' ({len(files)} файлов) ---",
                )
            )

            for file in files:
                # A rename entry is "old -> new" (see parse_git_status) - both
                # sides go in one pathspec so -A stages the old path's
                # deletion too, not just the new path's appearance. Passing
                # only the new path left the old one outside every pathspec
                # this tool ever ran, so its deletion never got staged by a
                # per-folder commit - permanently, if old and new live in
                # different folders and only one of them gets selected.
                pair = rename_parts(file)
                pathspec = list(pair) if pair else [file]
                self._run_git(
                    ["add", "-A", "--", *pathspec],
                    log_cmd=False,
                    log_output=False,
                    log_errors=True,
                )

            proc = self._run_git(
                ["commit", "-m", msg],
                log_cmd=True,
                log_output=True,
                log_errors=True,
            )

            if proc.returncode == 0:
                self.q.put(("log", f"Коммит для папки '{folder}' создан."))
            else:
                self.q.put(
                    (
                        "log",
                        f"Коммит для папки '{folder}' не создан "
                        "(возможно, нечего коммитить или есть конфликт).",
                    )
                )

        groups = self._collect_changes()
        self.q.put(("groups", groups))
        self.q.put(("done", "Коммит выбранных папок завершён."))

    def _worker_commit_all(self, msg: str) -> None:
        self._run_git(
            ["add", "-A"],
            log_cmd=True,
            log_output=False,
            log_errors=True,
        )

        proc = self._run_git(
            ["commit", "-m", msg],
            log_cmd=True,
            log_output=True,
            log_errors=True,
        )

        if proc.returncode == 0:
            self.q.put(("log", "Общий коммит создан."))
        else:
            self.q.put(("log", "Общий коммит не создан."))

        groups = self._collect_changes()
        self.q.put(("groups", groups))
        self.q.put(("done", "Общий коммит завершён."))

    def _worker_push(self) -> None:
        status = self._run_git(
            ["status", "--porcelain"],
            log_cmd=False,
            log_output=False,
            log_errors=False,
        )

        uncommitted = False
        if status.returncode == 0 and status.stdout.strip():
            uncommitted = True
            self.q.put(("log", "Внимание: есть незакоммиченные изменения."))

        branch_proc = self._run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"],
            log_cmd=False,
            log_output=False,
            log_errors=False,
        )

        branch_name = ""
        if branch_proc.returncode == 0:
            branch_name = branch_proc.stdout.strip()

        ahead_proc = self._run_git(
            ["rev-list", "--count", "origin/main..HEAD"],
            log_cmd=False,
            log_output=False,
            log_errors=False,
        )

        ahead_count = ""
        if ahead_proc.returncode == 0:
            ahead_count = ahead_proc.stdout.strip()

        # Логика близка к оригинальному скрипту:
        # если ветка main и нет новых коммитов относительно origin/main,
        # а также нет незакоммиченных изменений — пушить нечего.
        if (
            not uncommitted
            and branch_name == "main"
            and ahead_count.isdigit()
            and int(ahead_count) == 0
        ):
            self.q.put(("log", "Нет новых коммитов для пуша."))
            self.q.put(("done", "Push не требуется."))
            return

        push_proc = self._run_git(
            ["push"],
            log_cmd=True,
            log_output=True,
            log_errors=True,
        )

        if push_proc.returncode == 0:
            self.q.put(("log", "Push выполнен."))
            done_text = "Push выполнен."
        else:
            self.q.put(("log", "Push завершился с ошибкой."))
            done_text = "Push failed."

        # Обновить статус после push полезно, но не критично.
        groups = self._collect_changes()
        self.q.put(("groups", groups))
        self.q.put(("done", done_text))

    def _worker_publish_release(self, version: str, title: str, notes: str) -> None:
        """Tags public_dir's current HEAD and pushes the tag - the tag
        message carries BOTH the release title and its notes (read back by
        .github/workflows/release.yml via `git for-each-ref`), using the
        same subject/body convention as a commit message: `title` is the
        message's first line, then a blank line, then `notes` as the body -
        git's own %(contents:subject)/%(contents:body) split on exactly
        this shape, so the workflow needs no custom parsing. Everything past
        this push - build, zip, the actual GitHub Release - happens in
        Actions, not here."""
        existing = self._run_git(
            ["rev-parse", "-q", "--verify", f"refs/tags/{version}"],
            log_cmd=False,
            log_output=False,
            log_errors=False,
        )
        if existing.returncode == 0:
            self.q.put(("log", f"Тег {version} уже существует - публикация отменена."))
            self.q.put(("done", "Тег уже существует."))
            return

        status = self._run_git(
            ["status", "--porcelain"], log_cmd=False, log_output=False, log_errors=False
        )
        if status.returncode == 0 and status.stdout.strip():
            self.q.put((
                "log",
                "Внимание: в public-репозитории есть незакоммиченные изменения - "
                "тег всё равно встанет на текущий HEAD.",
            ))

        tag_proc = self._run_git(["tag", "-a", version, "-m", f"{title}\n\n{notes}"])
        if tag_proc.returncode != 0:
            self.q.put(("log", "Не удалось создать тег."))
            self.q.put(("done", "Ошибка создания тега."))
            return

        push_proc = self._run_git(["push", "origin", version])
        if push_proc.returncode == 0:
            self.q.put((
                "log",
                f"Тег {version} запушен - GitHub Actions начнёт сборку и публикацию релиза.",
            ))
            self.q.put(("done", f"Релиз {version} публикуется на GitHub."))
        else:
            self.q.put(("log", "Push тега завершился с ошибкой."))
            # Local tag rolled back so a retry doesn't immediately collide
            # with the "tag already exists" check above.
            self._run_git(
                ["tag", "-d", version], log_cmd=False, log_output=False, log_errors=False
            )
            self.q.put(("done", "Push тега не удался."))

    def _wait_for_release(self, version: str) -> bool:
        """Polls `gh release view` until `version` exists on GitHub, or gives
        up after ATTACH_WAIT_TIMEOUT_S. Shared by _worker_attach_files and
        _worker_create_rar - both need CI to have actually published the
        release before they have anything to act on, and pushing the tag
        only starts that build (routinely several minutes on windows-latest).
        Returns True once the release exists; False (having already queued
        the right log/done messages) if gh is missing or time ran out, so
        the caller's own worker can just `if not self._wait_for_release(...):
        return`. Logs a heartbeat every WAIT_HEARTBEAT_S - a build genuinely
        takes several minutes, and a log pane that prints one line and then
        sits silent for up to 20 is indistinguishable from one that hung."""
        started = time.monotonic()
        deadline = started + ATTACH_WAIT_TIMEOUT_S
        last_heartbeat = started
        announced_wait = False
        while True:
            check = self._run_process(
                ["gh", "release", "view", version, "--repo", REPO_SLUG],
                cwd=str(self.public_dir),
                log_cmd=False,
                log_output=False,
            )
            if check.returncode == 0:
                if announced_wait:
                    self.q.put(("log", f"Релиз «{version}» опубликован."))
                return True
            if check.returncode == 127:
                # _run_process already logged "Команда не найдена: gh".
                self.q.put(("done", "gh CLI не найден."))
                return False
            if time.monotonic() >= deadline:
                self.q.put((
                    "log",
                    f"Релиз «{version}» так и не появился на GitHub за "
                    f"{int(ATTACH_WAIT_TIMEOUT_S // 60)} мин. Возможно, сборка CI "
                    "ещё идёт или упала - проверьте вкладку Actions.",
                ))
                self.q.put(("done", "Релиз не дождались."))
                return False
            if not announced_wait:
                self.q.put((
                    "log",
                    f"Релиз «{version}» ещё не опубликован - ждём сборку GitHub Actions...",
                ))
                announced_wait = True
            elif time.monotonic() - last_heartbeat >= WAIT_HEARTBEAT_S:
                elapsed_min = int((time.monotonic() - started) // 60)
                self.q.put(("log", f"...всё ещё ждём (прошло {elapsed_min} мин)"))
                last_heartbeat = time.monotonic()
            time.sleep(ATTACH_WAIT_POLL_S)

    def _worker_attach_files(self, version: str, files: list[Path]) -> None:
        """Uploads each file as a release asset via `gh release upload`, not
        the GitHub API directly - gh already handles auth (its own stored
        token from `gh auth login`), so this script never has to hold a PAT
        of its own. --clobber lets re-running replace an asset a prior
        attempt (or CI) already uploaded, instead of failing on a name
        collision."""
        if not self._wait_for_release(version):
            return

        ok = True
        for path in files:
            proc = self._run_process(
                [
                    "gh", "release", "upload", version, str(path),
                    "--clobber", "--repo", REPO_SLUG,
                ],
                cwd=str(self.public_dir),
            )
            if proc.returncode == 0:
                self.q.put(("log", f"Загружен: {path.name}"))
            else:
                ok = False
                self.q.put(("log", f"Не удалось загрузить {path.name} (код {proc.returncode})."))

        self.q.put(("done", "Файлы прикреплены." if ok else "Часть файлов не загрузилась."))

    def _find_winrar(self) -> Path | None:
        for candidate in WINRAR_CANDIDATES:
            if candidate.is_file():
                return candidate
        return None

    def _worker_create_rar(self, version: str) -> None:
        """Builds a .rar from the SAME bytes CI already published, not a
        fresh local build - downloads the release's own .zip (gh release
        download), extracts it, re-packs the extracted folder with the
        local WinRAR install, and uploads the result back onto the release.
        Content-identical to the .zip by construction, which a separately
        run local `tools/build_portable.py` could not guarantee (different
        machine, possibly different dependency versions than the CI runner
        that actually built the release).

        CI itself never produces this: windows-latest has nothing that can
        WRITE .rar - it's a proprietary format only WinRAR's own licensed
        tool produces, unlike 7-Zip/Compress-Archive for .zip. Running an
        unlicensed WinRAR unattended in CI would also be its own can of
        worms - this only ever runs against the user's own local, already-
        licensed install."""
        winrar = self._find_winrar()
        if winrar is None:
            self.q.put((
                "log",
                "WinRAR не найден (искал: "
                + ", ".join(str(p) for p in WINRAR_CANDIDATES)
                + ") - установите WinRAR, чтобы создавать .rar.",
            ))
            self.q.put(("done", "WinRAR не найден."))
            return

        if not self._wait_for_release(version):
            return

        work_dir = Path(tempfile.mkdtemp(prefix="bridgebox_rar_"))
        try:
            self.q.put(("log", f"Скачиваем .zip релиза «{version}»..."))
            download = self._run_process(
                [
                    "gh", "release", "download", version,
                    "--repo", REPO_SLUG, "--pattern", "*.zip",
                    "--dir", str(work_dir), "--clobber",
                ],
                cwd=str(self.public_dir),
            )
            if download.returncode != 0:
                self.q.put(("done", "Не удалось скачать .zip релиза."))
                return

            zips = list(work_dir.glob("*.zip"))
            if not zips:
                self.q.put(("log", "gh ничего не скачал - у релиза нет .zip-ассета?"))
                self.q.put(("done", "Нет .zip для пересборки в .rar."))
                return

            extracted_dir = work_dir / "extracted"
            with zipfile.ZipFile(zips[0]) as archive:
                archive.extractall(extracted_dir)

            # The zip is itself wrapped in one BridgeBox_Portable-vX.Y.Z/
            # folder (see release.yml) - that's the single entry extraction
            # produces, and it becomes the .rar's own top-level folder too,
            # so both archives share the same shape a user sees on extract.
            top_level = [p for p in extracted_dir.iterdir() if p.is_dir()]
            if len(top_level) != 1:
                self.q.put((
                    "log",
                    f"Ожидалась ровно одна папка внутри .zip, а найдено {len(top_level)} - "
                    "структура архива не такая, как ожидалось.",
                ))
                self.q.put(("done", "Неожиданная структура .zip."))
                return
            folder_name = top_level[0].name

            rar_path = work_dir / f"{folder_name}.rar"
            self.q.put(("log", f"Упаковываем в {rar_path.name}..."))
            # cwd=extracted_dir + a bare relative folder name (not its full
            # path) is what makes WinRAR store `folder_name/...` as the
            # archive's own top-level entries, matching the .zip's shape -
            # passing the full path here would bake this machine's temp
            # directory into every entry's name instead.
            pack = self._run_process(
                [str(winrar), "a", "-r", str(rar_path), folder_name],
                cwd=str(extracted_dir),
            )
            if pack.returncode != 0 or not rar_path.exists():
                self.q.put(("done", "WinRAR завершился с ошибкой."))
                return

            self.q.put(("log", f"Загружаем {rar_path.name} на GitHub..."))
            upload = self._run_process(
                [
                    "gh", "release", "upload", version, str(rar_path),
                    "--clobber", "--repo", REPO_SLUG,
                ],
                cwd=str(self.public_dir),
            )
            if upload.returncode == 0:
                self.q.put(("log", f"Загружен: {rar_path.name}"))
                self.q.put(("done", f".rar для «{version}» создан и прикреплён."))
            else:
                self.q.put(("done", "Не удалось загрузить .rar на GitHub."))
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _worker_build_release(self) -> None:
        """Runs tools/build_portable.py end to end from public_dir (the
        public checkout) - PyInstaller, integrity baseline,
        validate_release()'s own problem-list check (raises on anything
        short of a complete, real build) - then zips AND rars the result
        into public_dir/dist/, same layout release.yml's CI build uses
        (wrapped in a BridgeBox_Portable-vX.Y.Z/ folder, matching filenames
        for both archives). This is the concrete answer to "how do I know
        it actually built": a failure here fails loudly (that SystemExit
        surfaces in the log, same as every other line the subprocess
        prints), and success ends with two real files on disk with real
        sizes, not a folder you have to go check by hand."""
        tree = self.public_dir
        python_exe = tree / "backend" / ".venv" / "Scripts" / "python.exe"
        script = tree / "tools" / "build_portable.py"
        icon = tree / ".github" / "assets" / "bridgebox.ico"

        cmd = [str(python_exe), str(script)]
        if icon.is_file():
            cmd += ["--icon", str(icon)]

        proc = self._run_process(cmd, cwd=str(tree))
        if proc.returncode != 0:
            self.q.put(("done", "Сборка провалилась - см. лог выше."))
            return

        release_dir = tree / "dist" / "BridgeBox_Portable"
        if not release_dir.is_dir():
            self.q.put(("log", f"Сборка отчиталась об успехе, но {release_dir} не найдена."))
            self.q.put(("done", "Собранная папка не найдена."))
            return

        pyproject = tree / "backend" / "pyproject.toml"
        match = _VERSION_RE.search(pyproject.read_text(encoding="utf-8")) if pyproject.exists() else None
        version = match.group(1) if match else "unknown"

        folder_name = f"BridgeBox_Portable-v{version}"
        versioned_dir = tree / "dist" / folder_name
        if versioned_dir.exists():
            shutil.rmtree(versioned_dir)
        release_dir.rename(versioned_dir)

        self.q.put(("log", f"Упаковываем {folder_name}.zip..."))
        shutil.make_archive(
            str(tree / "dist" / folder_name), "zip",
            root_dir=str(tree / "dist"), base_dir=folder_name,
        )
        zip_path = tree / "dist" / f"{folder_name}.zip"
        self.q.put(("log", f"Готово: {zip_path} ({zip_path.stat().st_size / 1024 / 1024:.1f} MB)"))

        winrar = self._find_winrar()
        if winrar is not None:
            rar_path = tree / "dist" / f"{folder_name}.rar"
            self.q.put(("log", f"Упаковываем {folder_name}.rar..."))
            pack = self._run_process(
                [str(winrar), "a", "-r", str(rar_path), folder_name],
                cwd=str(tree / "dist"),
            )
            if pack.returncode == 0 and rar_path.exists():
                self.q.put((
                    "log",
                    f"Готово: {rar_path} ({rar_path.stat().st_size / 1024 / 1024:.1f} MB)",
                ))
            else:
                self.q.put(("log", "WinRAR завершился с ошибкой - .rar не создан."))
        else:
            self.q.put((
                "log",
                "WinRAR не найден (искал: " + ", ".join(str(p) for p in WINRAR_CANDIDATES)
                + ") - создан только .zip.",
            ))

        self.q.put(("built_version", version))
        self.q.put(("done", f"Релиз {version} собран: {versioned_dir}"))


def main() -> None:
    root = tk.Tk()

    try:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        pass

    SyncPublishUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()