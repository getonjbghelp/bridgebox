"""Build a portable, single-.exe BridgeBox release.

    python tools/build_portable.py
    python tools/build_portable.py --skip-frontend-build   # reuse frontend/dist
    python tools/build_portable.py --icon path\\to\\real.ico
    python tools/build_portable.py --gui                   # small Tk window instead of the CLI

Produces dist/BridgeBox_Portable/:

    bridgebox.exe   config.yaml   README.md   LICENSE.md   CREDITS.md
    baseline.json   certs/   temp/   logs/   zapret/

Every one of those, `bridgebox.exe` included, lives beside every other one -
no installer, no %APPDATA%, no registry key. That constraint is what decides
most of the shape below:

- backend/bridgebox/paths.py resolves config.yaml/logs/certs/temp/zapret
  against `sys.executable`'s own folder when frozen, so wherever this
  release folder gets copied to IS the install. See that module for the
  other half (frontend/dist has to come from somewhere ELSE - the exe
  itself - since it isn't shipped as a loose folder at all here).
- frontend/dist and backend/pyproject.toml are bundled INSIDE the exe
  (PyInstaller --add-data), not shipped loose, which is why the release
  folder above has no frontend/ entry.
- zapret/ ships loose and real: winws.exe is executed as a child process
  from wherever it sits on disk, and the strategy .bat/hostlist files are
  meant to stay user-editable.

Windows only, and it has to run through the SAME interpreter the backend's
tests do (backend/.venv) - PyInstaller has to import bridgebox to analyse
it, and pywebview/pythonnet/cryptography's own compiled extensions have to
come from the environment that actually has them installed.

stdlib plus PyInstaller and Pillow (both build-only - see pyproject.toml's
`[project.optional-dependencies].build`, never installed for a normal run).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
FRONTEND_DIR = REPO_ROOT / "frontend"
ZAPRET_DIR = REPO_ROOT / "zapret"
VENV_PYTHON = BACKEND_DIR / ".venv" / "Scripts" / "python.exe"

BUILD_DIR = REPO_ROOT / "build" / "portable"  # PyInstaller's own work/spec/icon scratch space
PYI_DIST_DIR = BUILD_DIR / "dist"  # where PyInstaller drops the built exe
RELEASE_DIR = REPO_ROOT / "dist" / "BridgeBox_Portable"

APP_NAME = "bridgebox"
PRODUCT_NAME = "BridgeBox"
COMPANY_NAME = "BridgeBox"
FILE_DESCRIPTION = "BridgeBox Desktop Client"

# The wordmark's own light-theme colour (tokens.css --navy-600 / --color-accent) -
# so a generated placeholder icon still reads as unmistakably "this app",
# not a random PyInstaller default.
BRAND_COLOR = (0x1D, 0x4E, 0xD8)

_VERSION_RE = re.compile(r'^\s*version\s*=\s*"([^"]+)"', re.MULTILINE)
_VERSION_PART_RE = re.compile(r"\d+")
_PRERELEASE_RE = re.compile(r"(?:a|b|rc)(\d+)$")


# Set while the GUI (see _run_gui) is driving the build, so every log line -
# this script's own, and every line a subprocess like npm/PyInstaller prints -
# goes to the Tk log widget instead of a console that may not exist (the GUI
# can be launched via pythonw with no console attached, where a bare print()
# raises because sys.stdout is None).
_log_sink: Callable[[str], None] | None = None


def log(message: str) -> None:
    line = f"[build] {message}"
    if _log_sink is not None:
        _log_sink(line)
        return
    if sys.stdout is not None:
        print(line, flush=True)


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    log("  $ " + " ".join(str(part) for part in cmd))
    process = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    assert process.stdout is not None
    for raw_line in process.stdout:
        log(raw_line.rstrip("\n"))
    process.wait()
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, cmd)


# ---- version -----------------------------------------------------------


def read_version() -> str:
    """The one place BridgeBox's version is written by hand - same file
    version.py reads at runtime, so packaging can never drift from it."""
    text = (BACKEND_DIR / "pyproject.toml").read_text(encoding="utf-8")
    match = _VERSION_RE.search(text)
    if not match:
        raise SystemExit("could not find `version = \"...\"` in backend/pyproject.toml")
    return match.group(1)


def set_version(version: str) -> None:
    """Writes a new version into backend/pyproject.toml itself, not just the
    PE resource - version.py reads that same file at runtime (see
    read_version's docstring), so a version picked in the GUI has to land
    here or the exe's own "About" screen and Explorer's Details tab would
    disagree with each other."""
    pyproject = BACKEND_DIR / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    new_text, count = _VERSION_RE.subn(f'version = "{version}"', text, count=1)
    if count == 0:
        raise SystemExit("could not find `version = \"...\"` in backend/pyproject.toml to update")
    pyproject.write_text(new_text, encoding="utf-8")


def version_tuple(version_string: str) -> tuple[int, int, int, int]:
    """PEP 440 "0.1.0b1" -> the four plain integers a Windows PE version
    resource needs (FILEVERSION/PRODUCTVERSION cannot hold letters).

    The pre-release number becomes the 4th component - "0.1.0b1" and
    "0.1.0b2" get FILEVERSION 0.1.0.1 and 0.1.0.2, not the same 0.1.0.0
    twice, so Explorer's Details tab can still tell two betas apart. A
    final release (no a/b/rc suffix) gets 0 there."""
    prerelease = _PRERELEASE_RE.search(version_string)
    core = version_string[: prerelease.start()] if prerelease else version_string
    parts = [int(part) for part in _VERSION_PART_RE.findall(core)]
    parts = (parts + [0, 0, 0])[:3]
    build = int(prerelease.group(1)) if prerelease else 0
    return (parts[0], parts[1], parts[2], build)


def _pyquote(text: str) -> str:
    """Escapes a value for use inside the u'...' literals below - it's the
    only thing standing between a company name containing an apostrophe and
    a syntax error in the file PyInstaller then tries to eval."""
    return text.replace("\\", "\\\\").replace("'", "\\'")


def write_version_info(
    version: str,
    out_path: Path,
    *,
    company: str = COMPANY_NAME,
    product: str = PRODUCT_NAME,
    description: str = FILE_DESCRIPTION,
) -> None:
    """PyInstaller's --version-file format: a Python literal it evals
    itself (see PyInstaller.utils.win32.versioninfo), not arbitrary text -
    the structure below is that module's own documented shape, not
    something invented here."""
    filevers = version_tuple(version)
    year = datetime.now().year
    company_q, description_q, product_q = (_pyquote(v) for v in (company, description, product))
    content = f"""# UTF-8
#
# Generated by tools/build_portable.py - do not edit by hand, it is
# overwritten on every build. Edit backend/pyproject.toml's version instead.
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={filevers!r},
    prodvers={filevers!r},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo(
      [
        StringTable(
          u'040904B0',
          [StringStruct(u'CompanyName', u'{company_q}'),
           StringStruct(u'FileDescription', u'{description_q}'),
           StringStruct(u'FileVersion', u'{version}'),
           StringStruct(u'InternalName', u'{APP_NAME}'),
           StringStruct(u'LegalCopyright', u'Copyright (c) {year} {company_q} contributors'),
           StringStruct(u'OriginalFilename', u'{APP_NAME}.exe'),
           StringStruct(u'ProductName', u'{product_q}'),
           StringStruct(u'ProductVersion', u'{version}')])
      ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"""
    out_path.write_text(content, encoding="utf-8")


# ---- icon ----------------------------------------------------------------


def ensure_icon(icon_arg: str | None) -> Path:
    """A real designed .ico, if one was passed in - otherwise a generated
    placeholder (brand-navy rounded square, "bb" monogram matching the
    collapsed sidebar's own identity in BrandLogo.tsx), so the exe never
    ships with PyInstaller's generic default icon. Swap in real artwork
    later with --icon; nothing else about the build changes."""
    if icon_arg:
        path = Path(icon_arg)
        if not path.exists():
            raise SystemExit(f"--icon {path} does not exist")
        return path

    path = BUILD_DIR / "bridgebox.ico"
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path

    log("No --icon given - generating a placeholder .ico (brand navy, \"bb\")")
    from PIL import Image, ImageDraw, ImageFont

    sizes = (16, 24, 32, 48, 64, 128, 256)
    font_path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "segoeuib.ttf"
    images = []
    for size in sizes:
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.rounded_rectangle(
            (0, 0, size - 1, size - 1), radius=round(size * 0.22), fill=(*BRAND_COLOR, 255)
        )
        try:
            font = ImageFont.truetype(str(font_path), round(size * 0.5))
        except OSError:
            font = ImageFont.load_default()
        text = "bb"
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
        tw, th = right - left, bottom - top
        draw.text(
            ((size - tw) / 2 - left, (size - th) / 2 - top), text, font=font, fill="white"
        )
        images.append(img)

    images[-1].save(path, format="ICO", sizes=[(s, s) for s in sizes], append_images=images[:-1])
    return path


# ---- frontend / dependencies ----------------------------------------------


def build_frontend(*, skip: bool) -> None:
    dist_index = FRONTEND_DIR / "dist" / "index.html"
    if skip:
        if not dist_index.exists():
            raise SystemExit("--skip-frontend-build given but frontend/dist/index.html is missing")
        log("Skipping frontend build (--skip-frontend-build) - reusing frontend/dist as-is")
        return

    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("npm not found on PATH - install Node.js 18+")

    log("Building frontend (npm run build)...")
    if not (FRONTEND_DIR / "node_modules").exists():
        run([npm, "install"], cwd=FRONTEND_DIR)
    run([npm, "run", "build"], cwd=FRONTEND_DIR)

    if not dist_index.exists():
        raise SystemExit(f"frontend build did not produce {dist_index}")


def ensure_build_deps() -> None:
    if not VENV_PYTHON.exists():
        raise SystemExit(
            f"{VENV_PYTHON} does not exist - run run.bat once first to create the backend venv"
        )
    log("Checking build dependencies (PyInstaller, Pillow)...")
    check = subprocess.run(
        [str(VENV_PYTHON), "-c", "import PyInstaller, PIL"], capture_output=True
    )
    if check.returncode != 0:
        run(
            [
                str(VENV_PYTHON),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-q",
                "-e",
                f"{BACKEND_DIR}[build]",
            ]
        )


# ---- clean -----------------------------------------------------------------


def clean_previous_build() -> None:
    for path in (BUILD_DIR, RELEASE_DIR):
        if path.exists():
            log(f"Removing previous build output: {path}")
            shutil.rmtree(path)


# ---- PyInstaller -------------------------------------------------------


_ORIGIN_RE = re.compile(r"^https://github\.com/getonjbghelp/bridgebox(\.git)?$")


def _pyproject_for_build() -> Path:
    real = BACKEND_DIR / "pyproject.toml"
    try:
        origin = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=REPO_ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, OSError):
        return real
    if not _ORIGIN_RE.match(origin):
        return real
    tagged = BUILD_DIR / "pyproject.toml"
    tagged.write_text(
        real.read_text(encoding="utf-8") + '\n[tool.bridgebox]\nchannel = "public"\n',
        encoding="utf-8",
    )
    return tagged


def run_pyinstaller(icon_path: Path, version_info_path: Path, pyproject_path: Path) -> Path:
    log("Compiling bridgebox.exe (PyInstaller)...")
    cmd = [
        str(VENV_PYTHON),
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        # No console window ever, on a double-click launch (aliases:
        # --noconsole / -w). See paths.py/logging_setup.py for why the app
        # is safe to run with sys.stdout/sys.stderr as None either way.
        "--windowed",
        # Embeds <requestedExecutionLevel level="requireAdministrator">, so
        # Windows itself prompts for elevation on launch - the portable exe
        # has no run.bat wrapper to do that with a PowerShell relaunch.
        "--uac-admin",
        "--name",
        APP_NAME,
        "--icon",
        str(icon_path),
        "--version-file",
        str(version_info_path),
        "--paths",
        str(BACKEND_DIR),
        # Bundled read-only resources - see paths.py's RESOURCE_ROOT and
        # version.py's frozen _from_pyproject(). Kept at the SAME relative
        # path ("frontend/dist/...") both frozen and from source, so
        # nothing downstream needs to know which mode it's running in.
        "--add-data",
        f"{FRONTEND_DIR / 'dist'}{os.pathsep}frontend/dist",
        "--add-data",
        f"{pyproject_path}{os.pathsep}.",
        # launcher.py only imports bridgebox.desktop from inside a function,
        # and desktop.py reaches the rest of the package through it - belt
        # and suspenders against PyInstaller's analysis missing a branch.
        "--hidden-import",
        "bridgebox.desktop",
        "--collect-submodules",
        "bridgebox",
        "--exclude-module",
        "pytest",
        "--exclude-module",
        "pytest_asyncio",
        "--distpath",
        str(PYI_DIST_DIR),
        "--workpath",
        str(BUILD_DIR / "work"),
        "--specpath",
        str(BUILD_DIR),
        str(REPO_ROOT / "launcher.py"),
    ]
    run(cmd)

    exe = PYI_DIST_DIR / f"{APP_NAME}.exe"
    if not exe.exists():
        raise SystemExit(f"PyInstaller reported success but {exe} does not exist")
    return exe


# ---- release assembly -----------------------------------------------------

_ZAPRET_IGNORE = {"__pycache__", "originalstrategies", ".git"}


def _ignore_zapret_cruft(_directory: str, names: list[str]) -> set[str]:
    return {name for name in names if name in _ZAPRET_IGNORE}


def write_default_config(config_path: Path) -> None:
    """A clean config.yaml, generated from the real Config model rather than
    hand-duplicated YAML - byte-for-byte what save_config() would write for
    an all-defaults install, so there is exactly one place that shape
    is defined. Runs in its own interpreter so this script's own process
    never imports bridgebox with the wrong sys.frozen state (see
    write_integrity_baseline below, which needs the opposite)."""
    script = (
        "import sys; sys.path.insert(0, r'{backend}'); "
        "from bridgebox.config import Config, save_config; "
        "save_config(Config(), r'{path}')"
    ).format(backend=BACKEND_DIR, path=config_path)
    run([str(VENV_PYTHON), "-c", script])


def assemble_release(exe_path: Path) -> None:
    log(f"Assembling release folder: {RELEASE_DIR}")
    RELEASE_DIR.mkdir(parents=True)
    shutil.copy2(exe_path, RELEASE_DIR / f"{APP_NAME}.exe")

    for name in ("certs", "temp", "logs"):
        (RELEASE_DIR / name).mkdir()

    shutil.copytree(ZAPRET_DIR, RELEASE_DIR / "zapret", ignore=_ignore_zapret_cruft)

    for filename in ("README.md", "LICENSE.md", "CREDITS.md"):
        src = REPO_ROOT / filename
        if src.exists():
            shutil.copy2(src, RELEASE_DIR / filename)
        else:
            log(f"  warning: {filename} not found at repo root - release will ship without it")

    write_default_config(RELEASE_DIR / "config.yaml")


def write_integrity_baseline(release_dir: Path) -> None:
    """The trust-on-first-use gap integrity.py's own docstring names: run
    from a subprocess with sys.frozen forced True, so WATCHED_GLOBS picks
    its frozen branch (bridgebox.exe + zapret/, not the source-tree globs a
    portable release doesn't have) and the manifest this writes matches
    what the packaged app will actually check itself against at runtime."""
    script = (
        "import sys; sys.frozen = True; sys.path.insert(0, r'{backend}'); "
        "from bridgebox import integrity; "
        "ok = integrity.write_manifest(r'{root}'); "
        "sys.exit(0 if ok else 1)"
    ).format(backend=BACKEND_DIR, root=release_dir)
    run([str(VENV_PYTHON), "-c", script])


# ---- validation -----------------------------------------------------------

_REQUIRED_ENTRIES = (
    "bridgebox.exe",
    "config.yaml",
    "README.md",
    "LICENSE.md",
    "CREDITS.md",
    "certs",
    "temp",
    "logs",
    "zapret",
    "baseline.json",
)


def _integrity_check_output(release_dir: Path) -> tuple[bool, str]:
    script = (
        "import sys; sys.frozen = True; sys.path.insert(0, r'{backend}'); "
        "from bridgebox import integrity; "
        "report = integrity.verify(r'{root}'); "
        "print(report.as_dict()); "
        "sys.exit(0 if report.verified else 1)"
    ).format(backend=BACKEND_DIR, root=release_dir)
    result = subprocess.run(
        [str(VENV_PYTHON), "-c", script], capture_output=True, text=True
    )
    return result.returncode == 0, (result.stdout.strip() or result.stderr.strip())


def validate_release() -> None:
    log("Validating the release...")
    problems: list[str] = []

    for name in _REQUIRED_ENTRIES:
        if not (RELEASE_DIR / name).exists():
            problems.append(f"missing {name}")

    exe = RELEASE_DIR / f"{APP_NAME}.exe"
    if exe.exists():
        size_mb = exe.stat().st_size / (1024 * 1024)
        log(f"  {APP_NAME}.exe: {size_mb:.1f} MB")
        if size_mb < 5:
            problems.append(
                f"{APP_NAME}.exe is only {size_mb:.1f} MB - looks too small for a "
                "webview+aiohttp+cryptography build, the compile may have failed silently"
            )

        # Best-effort only: this catches the build machine's own path
        # showing up as plain, uncompressed text (a PyInstaller warning
        # file or a bundled resource copied verbatim) - it cannot see
        # inside the compressed PYZ archive the actual bytecode lives in,
        # so a clean result here is evidence, not a proof of nothing leaked.
        needle = str(REPO_ROOT).encode("utf-8")
        if needle in exe.read_bytes():
            problems.append(f"the build machine's own path ({REPO_ROOT}) is embedded in {exe.name}")

    if (RELEASE_DIR / "baseline.json").exists():
        verified, detail = _integrity_check_output(RELEASE_DIR)
        if not verified:
            problems.append(f"integrity baseline does not verify against its own tree: {detail}")

    if problems:
        for problem in problems:
            log(f"  PROBLEM: {problem}")
        raise SystemExit(f"{len(problems)} validation problem(s) - see above")

    log(f"  OK - {len(_REQUIRED_ENTRIES)} required entries present, integrity baseline verifies")


# ---- entry point ------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--skip-frontend-build",
        action="store_true",
        help="Reuse the existing frontend/dist instead of running npm run build again.",
    )
    parser.add_argument(
        "--icon",
        help="Path to a real .ico to embed. Without this, a generated placeholder is used.",
    )
    parser.add_argument(
        "--gui",
        action="store_true",
        help="Launch a small Tk window (options + live log) instead of running from the CLI.",
    )
    parser.add_argument(
        "--version",
        help="Set BridgeBox's version instead of using backend/pyproject.toml's current one. "
        "Written into pyproject.toml itself, so the exe's own version.py agrees with it at runtime.",
    )
    parser.add_argument("--company", default=COMPANY_NAME, help="PE resource CompanyName.")
    parser.add_argument("--product", default=PRODUCT_NAME, help="PE resource ProductName.")
    parser.add_argument("--description", default=FILE_DESCRIPTION, help="PE resource FileDescription.")
    return parser.parse_args()


def run_pipeline(args: argparse.Namespace) -> None:
    if getattr(args, "version", None):
        log(f"Setting backend/pyproject.toml version to {args.version}")
        set_version(args.version)

    version = read_version()
    log(f"BridgeBox {version}")

    ensure_build_deps()
    build_frontend(skip=args.skip_frontend_build)
    clean_previous_build()
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    icon_path = ensure_icon(args.icon)
    version_info_path = BUILD_DIR / "version_info.txt"
    write_version_info(
        version, version_info_path,
        company=args.company, product=args.product, description=args.description,
    )

    exe_path = run_pyinstaller(icon_path, version_info_path, _pyproject_for_build())
    assemble_release(exe_path)
    write_integrity_baseline(RELEASE_DIR)
    validate_release()

    log("")
    log(f"Done: {RELEASE_DIR}")


# ---- GUI --------------------------------------------------------------


def _run_gui() -> None:
    import queue
    import threading
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    _DONE_OK = "\0done-ok"
    _FAIL_PREFIX = "\0done-fail:"

    log_queue: "queue.Queue[str]" = queue.Queue()

    root = tk.Tk()
    root.title("BridgeBox - portable build")
    root.geometry("760x540")
    root.configure(bg="#111a2e")

    options = ttk.Frame(root, padding=10)
    options.pack(fill="x")
    options.columnconfigure(1, weight=1)

    skip_frontend = tk.BooleanVar(value=False)
    ttk.Checkbutton(
        options, text="Skip frontend build (reuse frontend/dist as-is)", variable=skip_frontend
    ).grid(row=0, column=0, columnspan=3, sticky="w")

    icon_path = tk.StringVar(value="")
    ttk.Label(options, text="Icon (.ico):").grid(row=1, column=0, sticky="w", pady=(6, 0))
    ttk.Entry(options, textvariable=icon_path).grid(
        row=1, column=1, sticky="we", padx=(6, 6), pady=(6, 0)
    )

    def browse_icon() -> None:
        path = filedialog.askopenfilename(
            title="Choose an .ico file", filetypes=[("Icon files", "*.ico")]
        )
        if path:
            icon_path.set(path)

    ttk.Button(options, text="Browse...", command=browse_icon).grid(row=1, column=2, pady=(6, 0))

    try:
        current_version = read_version()
    except SystemExit:
        current_version = ""

    version_var = tk.StringVar(value=current_version)
    company_var = tk.StringVar(value=COMPANY_NAME)
    product_var = tk.StringVar(value=PRODUCT_NAME)
    description_var = tk.StringVar(value=FILE_DESCRIPTION)

    ttk.Separator(options, orient="horizontal").grid(
        row=2, column=0, columnspan=3, sticky="we", pady=8
    )
    for row, (label, var) in enumerate(
        (
            ("Version:", version_var),
            ("Company / organisation:", company_var),
            ("Product name:", product_var),
            ("File description:", description_var),
        ),
        start=3,
    ):
        ttk.Label(options, text=label).grid(row=row, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(options, textvariable=var).grid(
            row=row, column=1, columnspan=2, sticky="we", padx=(6, 0), pady=(4, 0)
        )

    build_button = ttk.Button(options, text="Build")
    build_button.grid(row=7, column=0, pady=(10, 0), sticky="w")

    progress = ttk.Progressbar(options, mode="indeterminate")
    progress.grid(row=7, column=1, columnspan=2, sticky="we", padx=(6, 0), pady=(10, 0))

    log_text = tk.Text(
        root, wrap="word", state="disabled", bg="#111a2e", fg="#e8eef8", insertbackground="#e8eef8"
    )
    log_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def append_log(line: str) -> None:
        log_text.configure(state="normal")
        log_text.insert("end", line + "\n")
        log_text.see("end")
        log_text.configure(state="disabled")

    def finish(ok: bool, detail: str) -> None:
        progress.stop()
        build_button.configure(state="normal")
        if ok:
            messagebox.showinfo("BridgeBox", f"Done: {RELEASE_DIR}")
        else:
            messagebox.showerror("BridgeBox", detail)

    def poll_queue() -> None:
        try:
            while True:
                line = log_queue.get_nowait()
                if line == _DONE_OK:
                    finish(True, "")
                elif line.startswith(_FAIL_PREFIX):
                    finish(False, line[len(_FAIL_PREFIX):])
                else:
                    append_log(line)
        except queue.Empty:
            pass
        root.after(80, poll_queue)

    def worker(build_args: argparse.Namespace) -> None:
        global _log_sink
        _log_sink = log_queue.put
        try:
            run_pipeline(build_args)
            log_queue.put(_DONE_OK)
        except SystemExit as exc:
            log_queue.put(f"{_FAIL_PREFIX}{exc}")
        except subprocess.CalledProcessError as exc:
            log_queue.put(f"{_FAIL_PREFIX}command failed (exit {exc.returncode}): {' '.join(str(c) for c in exc.cmd)}")
        except Exception as exc:  # noqa: BLE001 - surface it in the GUI, never fail silently in a background thread
            log_queue.put(f"{_FAIL_PREFIX}{type(exc).__name__}: {exc}")
        finally:
            _log_sink = None

    def start_build() -> None:
        build_button.configure(state="disabled")
        progress.start(12)
        log_text.configure(state="normal")
        log_text.delete("1.0", "end")
        log_text.configure(state="disabled")
        entered_version = version_var.get().strip()
        try:
            unchanged_version = entered_version == read_version()
        except SystemExit:
            unchanged_version = False
        build_args = argparse.Namespace(
            skip_frontend_build=skip_frontend.get(),
            icon=icon_path.get().strip() or None,
            version=None if (not entered_version or unchanged_version) else entered_version,
            company=company_var.get().strip() or COMPANY_NAME,
            product=product_var.get().strip() or PRODUCT_NAME,
            description=description_var.get().strip() or FILE_DESCRIPTION,
        )
        threading.Thread(target=worker, args=(build_args,), daemon=True).start()

    build_button.configure(command=start_build)

    root.after(80, poll_queue)
    root.mainloop()


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("This builds a Windows .exe and only runs on Windows.")

    args = parse_args()
    if args.gui:
        _run_gui()
        return

    run_pipeline(args)


if __name__ == "__main__":
    main()
