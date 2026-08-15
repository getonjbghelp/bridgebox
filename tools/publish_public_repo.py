"""One-shot: turn githubpubliccode/ into its own git repo and push it as the
public BridgeBox source tree.

Run once, from the dev repo root:

    python tools/publish_public_repo.py

Safe to re-run: skips `git init`/`git commit` if a commit already exists,
and always asks for confirmation before the push itself.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DIR = REPO_ROOT / "githubpubliccode"
REMOTE_URL = "https://github.com/getonjbghelp/bridgebox.git"


def run(*args: str, cwd: Path) -> None:
    print(f"$ git {' '.join(args)}")
    subprocess.run(["git", *args], cwd=cwd, check=True)


def has_commits(repo: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo, capture_output=True,
    )
    return result.returncode == 0


def main() -> None:
    if not PUBLIC_DIR.is_dir():
        sys.exit(f"{PUBLIC_DIR} does not exist - nothing to publish.")

    git_dir = PUBLIC_DIR / ".git"
    if not git_dir.is_dir():
        run("init", cwd=PUBLIC_DIR)
        run("branch", "-M", "main", cwd=PUBLIC_DIR)

    remotes = subprocess.run(
        ["git", "remote"], cwd=PUBLIC_DIR, capture_output=True, text=True,
    ).stdout.split()
    if "origin" not in remotes:
        run("remote", "add", "origin", REMOTE_URL, cwd=PUBLIC_DIR)

    if not has_commits(PUBLIC_DIR):
        run("add", ".", cwd=PUBLIC_DIR)
        run("commit", "-m", "Initial public release", cwd=PUBLIC_DIR)
    else:
        print("Repo already has a commit - skipping add/commit.")
        print("Re-sync githubpubliccode/ from the dev tree yourself first if it's stale,")
        print("then run `git add -A && git commit` inside it before pushing.")

    answer = input(
        f"\nPush {PUBLIC_DIR.name}'s main branch to {REMOTE_URL} now? [y/N] "
    ).strip().lower()
    if answer != "y":
        print("Not pushing. Re-run this script when ready.")
        return

    run("push", "-u", "origin", "main", cwd=PUBLIC_DIR)
    print("\nDone. Check the repo on GitHub, then follow the post-push checklist")
    print("from the publishing guide (description/topics, Actions permissions,")
    print("first Release with the portable zip).")


if __name__ == "__main__":
    main()
