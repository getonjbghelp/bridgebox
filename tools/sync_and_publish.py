"""Re-sync githubpubliccode/ from the dev tree and push the changes to the
public repo.

Run whenever program code (not personal/dev-only files) changed and the
public mirror needs to catch up:

    python tools/sync_and_publish.py
    python tools/sync_and_publish.py -m "Fix connection retry bug"

Copies the dev tree into githubpubliccode/ with robocopy (skipping the same
build artifacts, caches, and personal files publish_public_repo.py's
original copy skipped - see EXCLUDE_DIRS/EXCLUDE_FILES below), then commits
and pushes from inside githubpubliccode/. Asks for confirmation before the
push; safe to re-run with nothing changed (exits early).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIR = REPO_ROOT / "githubpubliccode"

EXCLUDE_DIRS = [
    ".venv", "node_modules", "__pycache__", ".pytest_cache", "dist", "build",
    "logs", "certs", "temp", "graphify-out", ".claude", ".impeccable", ".git",
    "githubpubliccode", "docs", "*.egg-info", "handoffs", "problems-BB", "tgbot",
    "scuba",
]
EXCLUDE_FILES = [
    "config.yaml", "baseline.json", "HANDOFF-*.md",
    "ОБЪЯСНЕНИЕ_ФАЙЛОВ.txt", "cmd.exe",
    # Chat moderation notes, not project source - no reason a GitHub visitor
    # needs these.
    "TELEGRAM_RULES.md", "TELEGRAM_RULES.txt",
    # Maintained directly in the public repo from here on - see its own copy.
    "build_portable.py",
]


def sync() -> None:
    if not PUBLIC_DIR.is_dir():
        sys.exit(f"{PUBLIC_DIR} does not exist - run tools/publish_public_repo.py first.")

    result = subprocess.run(
        [
            "robocopy", str(REPO_ROOT), str(PUBLIC_DIR), "/E",
            "/XD", *EXCLUDE_DIRS,
            "/XF", *EXCLUDE_FILES,
        ],
        capture_output=True, text=True,
    )
    print(result.stdout)
    if result.returncode >= 8:
        print(result.stderr)
        sys.exit(f"robocopy failed (exit {result.returncode})")


def run(*args: str) -> None:
    print(f"$ git {' '.join(args)}")
    subprocess.run(["git", *args], cwd=PUBLIC_DIR, check=True)


def has_changes() -> bool:
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=PUBLIC_DIR, capture_output=True, text=True,
    ).stdout
    return bool(status.strip())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-m", "--message", default="Sync from dev tree")
    args = parser.parse_args()

    sync()

    if not has_changes():
        print("Nothing changed since the last publish - nothing to commit.")
        return

    run("add", "-A")
    run("status", "--short")
    run("commit", "-m", args.message)

    answer = input("\nPush to origin/main now? [y/N] ").strip().lower()
    if answer != "y":
        print("Committed locally, not pushed. Run `git push` inside githubpubliccode/ when ready.")
        return

    run("push")


if __name__ == "__main__":
    main()
