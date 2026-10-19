"""`taura diff` and `taura publish`: what a build changes in the paper, and applying it."""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from . import NAME, gitutil
from . import lock as lockmod
from .build import Built, build, code_commit
from .checks import check_hand_edits
from .discovery import LOCK_FILE, Pair, contract
from .errors import Refusal
from .generate import parse, parse_macros, parse_table, plain
from .hashing import content_hash
from .ledger import Ledger


@dataclass
class Change:
    artifact: str
    change: str  # added | removed | changed | regenerated | new | deleted
    what: str  # human-readable subject: "\res{mba/f1/mean}", "row MBA col F1", ...
    old: str | None = None
    new: str | None = None

    def __str__(self) -> str:
        if self.change == "changed":
            return f"{self.artifact} {self.what}: {self.old} → {self.new}"
        if self.change == "added":
            return f"{self.artifact} {self.what}: added ({self.new})"
        if self.change == "removed":
            return f"{self.artifact} {self.what}: removed (was {self.old})"
        return f"{self.artifact}: {self.change}" + (f" ({self.what})" if self.what else "")


def _keyed(old: dict[str, str], new: dict[str, str], art: str, fmt) -> list[Change]:
    out = []
    for k in sorted(old.keys() | new.keys(), key=lambda k: (k not in old, k)):
        if k not in new:
            out.append(Change(art, "removed", fmt(k), old=old[k]))
        elif k not in old:
            out.append(Change(art, "added", fmt(k), new=new[k]))
        elif old[k] != new[k]:
            out.append(Change(art, "changed", fmt(k), old[k], new[k]))
    return out


def _table_changes(art: str, old_body: str, new_body: str) -> list[Change]:
    oh, orows = parse_table(old_body)
    nh, nrows = parse_table(new_body)
    if not oh or not nh:
        return [Change(art, "regenerated", "")]
    out = []
    if oh != nh:
        out.append(Change(art, "changed", "columns", " | ".join(oh), " | ".join(nh)))
    old = {r[0]: r for r in orows if r}
    new = {r[0]: r for r in nrows if r}
    for label in [r[0] for r in orows if r and r[0] not in new]:
        out.append(Change(art, "removed", f"row {plain(label)}", old=" | ".join(plain(c) for c in old[label][1:])))
    for r in nrows:
        label = r[0]
        if label not in old:
            out.append(Change(art, "added", f"row {plain(label)}", new=" | ".join(plain(c) for c in r[1:])))
            continue
        for j, cell in enumerate(r[1:], 1):
            prev = old[label][j] if j < len(old[label]) else ""
            if cell != prev:
                col = plain(nh[j]) if j < len(nh) else str(j)
                if plain(cell) == plain(prev):  # only the bolding moved
                    what, a, b = f"row {plain(label)} col {col} bold", str(prev.startswith("\\textbf")), str(cell.startswith("\\textbf"))
                else:
                    what, a, b = f"row {plain(label)} col {col}", plain(prev), plain(cell)
                out.append(Change(art, "changed", what, a, b))
    return out or [Change(art, "regenerated", "layout")]


def changes_for(built: Built, paper: Path) -> list[Change]:
    art = built.artifact
    dest = paper / art.output
    if not dest.is_file():
        return [Change(art.name, "new", art.output)]
    if art.is_binary:
        return [] if content_hash(dest) == built.hash else [Change(art.name, "regenerated", "")]
    old = parse(dest.read_text(encoding="utf-8", errors="replace"))
    if old is not None and old.actual_hash == built.hash:
        return []
    new_body = parse(built.path.read_text(encoding="utf-8")).body
    old_body = old.body if old else dest.read_text(encoding="utf-8", errors="replace")
    if art.kind == "macros":
        return _keyed(parse_macros(old_body), parse_macros(new_body), art.name, lambda k: f"\\res{{{k}}}") \
            or [Change(art.name, "regenerated", "layout")]
    if art.kind == "table":
        return _table_changes(art.name, old_body, new_body)
    return [Change(art.name, "regenerated", "")]


@dataclass
class Plan:
    built: list[Built]
    changes: list[Change]
    to_copy: list[Built]
    to_delete: list[str]  # outputs of artifacts removed from artifacts.toml


def _lock_entry(lock: dict, b: Built) -> tuple[dict | None, dict]:
    entry = lock["artifacts"].get(b.artifact.name)
    return entry, {r: lock["results"].get(r) for r in b.inputs}


def plan(pair: Pair) -> Plan:
    paper = pair.require_paper()
    ledger = Ledger(pair.code)
    built = build(ledger)
    lock = lockmod.read(paper)
    changes, to_copy = [], []
    for b in built:
        c = changes_for(b, paper)
        changes += c
        if c:
            to_copy.append(b)
        elif _lock_entry(lock, b) != (b.lock_entry(), b.inputs):
            changes.append(Change(b.artifact.name, "provenance", "inputs changed, output identical"))
    current = {b.artifact.name for b in built}
    to_delete = []
    for name, entry in sorted(lock["artifacts"].items()):
        if name not in current:
            changes.append(Change(name, "deleted", entry["output"]))
            to_delete.append(entry["output"])
    return Plan(built, changes, to_copy, to_delete)


def diff(pair: Pair) -> list[Change]:
    return plan(pair).changes


# ---- publish ---------------------------------------------------------------------


def preflight(pair: Pair, allow_dirty_code: bool, fetch: bool) -> list[str]:
    """Reasons publishing must not go ahead (empty = OK)."""
    paper = pair.require_paper()
    problems = []
    if not gitutil.is_repo(paper):
        return [f"paper {paper} is not a git repo"]
    dirty = gitutil.status_lines(paper)
    if dirty:
        problems.append("paper repo has uncommitted changes:\n  " + "\n  ".join(dirty[:10]))
    if fetch and gitutil.upstream(paper):
        r = gitutil.fetch(paper)
        if r.returncode != 0:
            problems.append(f"`git fetch` in the paper repo failed ({r.stderr.strip() or 'no output'}); "
                            "retry, or pass --no-fetch to publish without checking the remote")
        else:
            n = gitutil.behind_count(paper)
            if n:
                problems.append(f"paper repo is {n} commit(s) behind {gitutil.upstream(paper)}: "
                                "run `git pull` in the paper repo (a collaborator has edited on Overleaf), then `taura check`")
    edits = check_hand_edits(paper, lockmod.read(paper))
    for f in edits:
        problems.append(f"{f.message} — {f.fix}")
    if gitutil.is_repo(pair.code) and gitutil.is_dirty(pair.code) and not allow_dirty_code:
        problems.append("code repo has uncommitted changes; commit them first (or pass --allow-dirty-code)")
    return problems


def publish(pair: Pair, allow_dirty_code: bool = False, fetch: bool = True, dry_run: bool = False) -> dict:
    paper = pair.require_paper()
    problems = preflight(pair, allow_dirty_code, fetch)
    if problems:
        raise Refusal("refusing to publish:\n- " + "\n- ".join(problems))
    p = plan(pair)
    sha, _ = code_commit(pair.code)
    result = {"changes": [asdict(c) | {"text": str(c)} for c in p.changes], "commit": None, "dry_run": dry_run}
    if not p.changes or dry_run:
        return result

    for b in p.to_copy:
        dest = paper / b.artifact.output
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(b.path, dest)
    for rel in p.to_delete:
        (paper / rel).unlink(missing_ok=True)

    lock = lockmod.read(paper)
    lock["code_repo"] = contract(pair.code)
    lock["code_commit"] = sha
    lock["published"] = datetime.now().astimezone().isoformat(timespec="seconds")
    lock["artifacts"] = {b.artifact.name: b.lock_entry() for b in p.built}
    lock["results"] = {r: h for b in p.built for r, h in b.inputs.items()}
    lockmod.write(paper, lock)

    paths = [LOCK_FILE] + [b.artifact.output for b in p.to_copy]
    gitutil.git(paper, "add", "--", *paths)
    if p.to_delete:
        gitutil.git(paper, "rm", "--cached", "--quiet", "--ignore-unmatch", "--", *p.to_delete)
    lines = [str(c) for c in p.changes]
    shown = lines[:40] + ([f"... and {len(lines) - 40} more"] if len(lines) > 40 else [])
    msg = f"{NAME} publish from {pair.code.name}@{sha[:7]}\n\n" + "\n".join(f"- {l}" for l in shown)
    gitutil.git(paper, "commit", "--quiet", "-m", msg)
    result["commit"] = gitutil.head(paper)
    return result
