"""taura.lock: provenance of everything in the paper's generated/ folder."""

from __future__ import annotations

from pathlib import Path

from . import tomlio
from .discovery import LOCK_FILE


def path(paper: Path) -> Path:
    return paper / LOCK_FILE


def read(paper: Path) -> dict:
    p = path(paper)
    lock = tomlio.load(p) if p.is_file() else {}
    lock.setdefault("results", {})
    lock.setdefault("artifacts", {})
    return lock


def write(paper: Path, lock: dict) -> None:
    ordered = {k: lock[k] for k in ("code_repo", "code_commit", "published") if k in lock}
    ordered["results"] = dict(sorted(lock.get("results", {}).items()))
    ordered["artifacts"] = dict(sorted(lock.get("artifacts", {}).items()))
    path(paper).write_text(tomlio.dumps(ordered), encoding="utf-8")


def outputs(lock: dict) -> dict[str, str]:
    """output path -> artifact name."""
    return {a["output"]: name for name, a in lock["artifacts"].items()}
