from __future__ import annotations

import os
import subprocess
from pathlib import Path

# Paths taura itself writes inside the code repo; never count them as dirt.
STAGING_DIR = ".taura"


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=check, env=env
    )


def is_repo(path: Path) -> bool:
    return path.is_dir() and git(path, "rev-parse", "--git-dir", check=False).returncode == 0


def head(repo: Path) -> str | None:
    r = git(repo, "rev-parse", "HEAD", check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def is_dirty(repo: Path) -> bool:
    r = git(repo, "status", "--porcelain", "--", ".", f":(exclude){STAGING_DIR}", check=False)
    return bool(r.stdout.strip())


def status_lines(repo: Path) -> list[str]:
    return [l for l in git(repo, "status", "--porcelain", check=False).stdout.splitlines() if l]


def upstream(repo: Path) -> str | None:
    r = git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def fetch(repo: Path) -> subprocess.CompletedProcess:
    return git(repo, "fetch", "--quiet", check=False)


def behind_count(repo: Path) -> int:
    r = git(repo, "rev-list", "--count", "HEAD..@{u}", check=False)
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else 0


def last_commit_touching(repo: Path, path: str) -> str | None:
    r = git(repo, "log", "-1", "--format=%H", "--", path, check=False)
    return r.stdout.strip() or None


def show(repo: Path, rev: str, path: str) -> bytes | None:
    r = subprocess.run(
        ["git", "-C", str(repo), "show", f"{rev}:{path}"], capture_output=True, check=False
    )
    return r.stdout if r.returncode == 0 else None


def is_dirty_except(repo: Path, paths: list[str]) -> bool:
    """Uncommitted changes outside taura's staging area and the given paths (e.g. ledger/runs)."""
    excludes = [f":(exclude){p}" for p in [STAGING_DIR, *paths]]
    r = git(repo, "status", "--porcelain", "--", ".", *excludes, check=False)
    return bool(r.stdout.strip())
