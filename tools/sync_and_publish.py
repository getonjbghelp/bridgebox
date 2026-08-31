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
import subprocess
import sys
import threading
import traceback
from collections import defaultdict
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
from tkinter.scrolledtext import ScrolledText


EXCLUDE_DIRS = [
    "example"
]

EXCLUDE_FILES = [
    "example"
]


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
        self.msg_var = tk.StringVar(value="Sync from dev tree")
        self.ask_per_folder_var = tk.BooleanVar(value=False)

        self._build_ui()
        self._poll_queue()
        self.refresh_changes()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        main = ttk.Frame(self.root, padding=8)
        main.pack(fill="both", expand=True)

        # Пути
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

        # Кнопки
        toolbar1 = ttk.Frame(main)
        toolbar1.pack(fill="x", pady=(8, 0))

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

        toolbar2 = ttk.Frame(main)
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
        commit_frame = ttk.LabelFrame(main, text="Коммит", padding=6)
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

        # Область изменений и лог
        pane = ttk.Panedwindow(main, orient="vertical")
        pane.pack(fill="both", expand=True, pady=(8, 0))

        selection_frame = ttk.Frame(pane)
        pane.add(selection_frame, weight=3)

        split = ttk.Panedwindow(selection_frame, orient="horizontal")
        split.pack(fill="both", expand=True)

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

        # Лог
        log_frame = ttk.Frame(pane)
        pane.add(log_frame, weight=1)

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

        default_msg = self.msg_var.get().strip() or "Sync from dev tree"

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
                initialvalue="Sync from dev tree",
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