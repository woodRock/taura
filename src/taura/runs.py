"""Run records: ledger/runs/<id>.toml, one per experiment run.

Written by `taura run -- <cmd>` (a command on this machine) or `taura record <id> ...` (a command
run elsewhere, e.g. by gpu-spooler's daemon). A record holds the commit and dirty flag the run
used, the command, its config file and seed, the host, the datasets (label + content hash), and the
outputs it wrote with their content hashes. Results link to runs by those hashes: a result whose
source file has the hash a run wrote came from that run.

Datasets are declared in taura.toml:
    [data.fish-oil]
    path = "data/fish_oil"      # relative to the code repo; symlinks are followed
    label = "v1"                # for people; the hash is what gets checked
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import gitutil, tomlio
from .discovery import CONFIG_FILE
from .errors import TauraError
from .hashing import content_hash, sha256_bytes, sha256_file
from .ledger import LEDGER, RUNS_DIR

CACHE = Path(gitutil.STAGING_DIR) / "data-cache.json"
CODE_SUFFIXES = (".py", ".sh", ".r", ".jl", ".m", ".ipynb")
CONFIG_SUFFIXES = (".yaml", ".yml", ".json", ".toml", ".cfg", ".ini")
SKIP_DIRS = {".git", gitutil.STAGING_DIR, "__pycache__", ".venv", "venv", "node_modules", ".mypy_cache",
             ".pytest_cache", ".ruff_cache"}
MAX_SCAN = 200_000  # files: past this, output detection is skipped (declare --outputs instead)


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def iso(t: str | float | None) -> str | None:
    """An epoch seconds value or ISO string, as local ISO time."""
    if t is None or t == "":
        return None
    try:
        return datetime.fromtimestamp(float(t), timezone.utc).astimezone().isoformat(timespec="seconds")
    except ValueError:
        return datetime.fromisoformat(str(t)).isoformat(timespec="seconds")


def new_id(prefix: str = "") -> str:
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    return f"{stamp}-{prefix}{secrets.token_hex(2)}"


# ---- records ---------------------------------------------------------------------------

ID = re.compile(r"^[A-Za-z0-9_.\-]+$")


def path(code: Path, run_id: str) -> Path:
    if not ID.match(run_id):
        raise TauraError(f"run id {run_id!r} may only use letters, digits, _ . -")
    return code / RUNS_DIR / f"{run_id}.toml"


def load(code: Path, run_id: str) -> dict | None:
    p = path(code, run_id)
    return tomlio.load(p) if p.is_file() else None


def load_all(code: Path) -> dict[str, dict]:
    rdir = code / RUNS_DIR
    out = {}
    for p in sorted(rdir.glob("*.toml")) if rdir.is_dir() else []:
        rec = tomlio.load(p)
        out[rec.get("id", p.stem)] = rec
    return out


ORDER = ("id", "status", "started", "finished", "exit_code", "commit", "base_commit", "dirty", "command", "cwd",
         "config_path", "config_hash", "seed", "host", "gpus", "spooler_job", "runner", "inputs", "data",
         "outputs")


def save(code: Path, rec: dict) -> Path:
    p = path(code, rec["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    ordered = {k: rec[k] for k in ORDER if rec.get(k) is not None}
    ordered |= {k: v for k, v in rec.items() if k not in ordered and v is not None}
    p.write_text(tomlio.dumps(ordered), encoding="utf-8")
    return p


# ---- what the command names ------------------------------------------------------------------


def parse_command(code: Path, argv: list[str], base: Path | None = None) -> dict:
    """Config file (--config/-c), seed (--seed) and the code/config files a command names.

    Relative paths in the command are resolved from base (the command's working directory)."""
    base = base or code
    out: dict = {}
    for i, a in enumerate(argv):
        for flag, key in (("--config", "config_path"), ("-c", "config_path"), ("--seed", "seed")):
            if a == flag and i + 1 < len(argv):
                out.setdefault(key, argv[i + 1])
            elif a.startswith(flag + "=") and flag.startswith("--"):
                out.setdefault(key, a.split("=", 1)[1])
    if "seed" in out:
        try:
            out["seed"] = int(out["seed"])
        except ValueError:
            pass
    inputs = []
    for a in argv:
        f = (base / a) if not os.path.isabs(a) else Path(a)
        if f.suffix.lower() in CODE_SUFFIXES + CONFIG_SUFFIXES and f.is_file():
            try:
                inputs.append(str(f.resolve().relative_to(code.resolve())))
            except ValueError:
                inputs.append(str(f))
    if inputs:
        out["inputs"] = sorted(set(inputs))
    cfg = out.get("config_path")
    if cfg and (base / cfg).is_file():
        out["config_hash"] = sha256_file(base / cfg)
        try:
            out["config_path"] = str((base / cfg).resolve().relative_to(code.resolve()))
        except ValueError:
            pass
    elif cfg:
        out.pop("config_path")
    return out


# ---- git ------------------------------------------------------------------------------------


def git_state(code: Path, commit: str | None = None, dirty_count: int | None = None) -> dict:
    """commit, dirty, and base_commit when the commit is a snapshot of a dirty tree.

    gpu-spooler syncs a dirty tree as a `git stash create` commit (two parents, "gpu-spooler sync"):
    that is recorded as dirty, with the commit it was made on top of."""
    if not gitutil.is_repo(code):
        return {"commit": commit or "unknown", "dirty": bool(dirty_count)}
    if commit is None:
        commit = gitutil.head(code) or "unknown"
        dirty = bool(uncommitted_code(code))
    else:
        dirty = bool(dirty_count)
    out = {"commit": commit, "dirty": dirty}
    r = gitutil.git(code, "log", "-1", "--format=%P%n%s", commit, check=False)
    if r.returncode == 0:
        parents, _, subject = r.stdout.partition("\n")
        if len(parents.split()) >= 2 and "gpu-spooler sync" in subject:
            out["dirty"], out["base_commit"] = True, parents.split()[0]
    return out


def uncommitted_code(code: Path) -> list[str]:
    """Uncommitted changes that could affect a run: everything except taura's own files (ledger/,
    .taura/) and files whose content is the recorded output of a run (an earlier step of a pipeline
    rewriting a tracked report is not a code edit)."""
    r = gitutil.git(code, "status", "--porcelain", "--", ".", f":(exclude){gitutil.STAGING_DIR}",
                    f":(exclude){LEDGER}", check=False)
    outputs = set(Index(code).by_hash) if r.stdout.strip() else set()
    changed = []
    for line in r.stdout.splitlines():
        rel = line[3:].split(" -> ")[-1].strip('"')
        f = code / rel
        if f.is_file() and content_hash(f) in outputs:
            continue
        changed.append(rel)
    return changed


def commit_exists(code: Path, commit: str) -> bool:
    return gitutil.git(code, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode == 0


def changed_since(code: Path, commit: str, paths: list[str]) -> list[str]:
    """Which of paths differ between commit and the working tree now."""
    if not paths or not commit_exists(code, commit):
        return []
    r = gitutil.git(code, "diff", "--name-only", commit, "--", *paths, check=False)
    return sorted(set(r.stdout.split())) if r.returncode == 0 else []


# ---- data -----------------------------------------------------------------------------------


def datasets(code: Path) -> dict[str, dict]:
    cfg_path = code / CONFIG_FILE
    cfg = tomlio.load(cfg_path) if cfg_path.is_file() else {}
    out = {}
    for name, spec in cfg.get("data", {}).items():
        if not isinstance(spec, dict) or "path" not in spec:
            raise TauraError(f"{cfg_path}: [data.{name}] needs a `path`")
        out[name] = spec
    return out


def hash_dir(root: Path, cache: dict) -> tuple[str, int]:
    """Content hash of every file under root (symlinks followed), reusing cached file hashes
    whose size and mtime haven't changed. Returns (hash, file count)."""
    entries = []
    for dirpath, dirs, files in os.walk(root, followlinks=True):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for f in sorted(files):
            if f.startswith("."):
                continue
            p = Path(dirpath) / f
            st = p.stat()
            key = str(p.resolve())
            hit = cache.get(key)
            if hit and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
                h = hit[2]
            else:
                h = sha256_file(p)
                cache[key] = [st.st_size, st.st_mtime_ns, h]
            entries.append(f"{p.relative_to(root)}\0{h}")
    return sha256_bytes("\n".join(entries).encode()), len(entries)


def data_state(code: Path) -> dict[str, dict]:
    sets = datasets(code)
    if not sets:
        return {}
    cache_file = code / CACHE
    try:
        cache = json.loads(cache_file.read_text()) if cache_file.is_file() else {}
    except json.JSONDecodeError:
        cache = {}
    out = {}
    for name, spec in sets.items():
        root = (code / spec["path"]).expanduser()
        entry = {"path": spec["path"]}
        if "label" in spec:
            entry["label"] = str(spec["label"])
        if root.exists():
            entry["hash"], entry["files"] = hash_dir(root, cache) if root.is_dir() else (sha256_file(root), 1)
        else:
            entry["missing"] = True
        out[name] = entry
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache))
    return out


# ---- outputs --------------------------------------------------------------------------------


@dataclass
class Snapshot:
    files: dict[str, tuple[int, int]] | None  # rel path -> (mtime_ns, size); None if too many to scan


def scan(code: Path) -> Snapshot:
    files: dict[str, tuple[int, int]] = {}
    for dirpath, dirs, names in os.walk(code):  # symlinked dirs (e.g. data) are not followed
        rel_dir = Path(dirpath).relative_to(code)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")
                   and str(rel_dir / d) != RUNS_DIR]
        for n in names:
            p = Path(dirpath) / n
            try:
                st = p.lstat()
            except OSError:
                continue
            files[str(rel_dir / n)] = (st.st_mtime_ns, st.st_size)
            if len(files) > MAX_SCAN:
                return Snapshot(None)
    return Snapshot(files)


def outputs(code: Path, before: Snapshot, after: Snapshot, declared: list[str]) -> dict[str, str]:
    """Files the run created or changed (plus any declared), with their content hashes."""
    rels = set()
    if before.files is not None and after.files is not None:
        rels = {p for p, st in after.files.items() if before.files.get(p) != st}
    rels |= {str(Path(d)) for d in declared}
    out = {}
    for rel in sorted(rels):
        f = code / rel
        if f.is_file():
            out[rel] = content_hash(f)
    return out


def hash_outputs(code: Path, paths: list[str]) -> dict[str, str]:
    """Content hashes of files or of every file under directories (for `taura record --outputs`)."""
    out = {}
    for p in paths:
        f = code / p
        files = [f] if f.is_file() else sorted(x for x in f.rglob("*") if x.is_file()) if f.is_dir() else []
        for x in files:
            out[str(x.relative_to(code))] = content_hash(x)
    return out


# ---- the record a run starts with -------------------------------------------------------------


def start_record(code: Path, run_id: str, argv: list[str], command: str | None = None, cwd: Path | None = None,
                 **extra) -> dict:
    rec = {"id": run_id, "status": "running", "started": now(),
           "command": command or " ".join(shlex.quote(a) for a in argv),
           "host": socket.gethostname()}
    if cwd is not None and cwd.resolve() != code.resolve():
        try:
            rec["cwd"] = str(cwd.resolve().relative_to(code.resolve()))
        except ValueError:
            rec["cwd"] = str(cwd)
    rec |= parse_command(code, argv, cwd)
    rec |= extra
    rec["data"] = data_state(code) or None
    return rec


# ---- linking results to runs --------------------------------------------------------------


def watch_paths(code: Path) -> list[str]:
    """Extra code paths whose change since a run makes its results suspect ([runs] watch in taura.toml)."""
    cfg_path = code / CONFIG_FILE
    cfg = tomlio.load(cfg_path) if cfg_path.is_file() else {}
    return list(cfg.get("runs", {}).get("watch", []))


class Index:
    """All run records of a code repo, and which runs wrote which content."""

    def __init__(self, code: Path):
        self.code = code
        self.records = load_all(code)
        self.by_hash: dict[str, list[str]] = {}
        for rid, rec in self.records.items():
            for h in (rec.get("outputs") or {}).values():
                self.by_hash.setdefault(h, []).append(rid)

    def linked(self, explicit: list[str] | None, content: str | None) -> list[tuple[str, str]]:
        """(run id, how) for runs named explicitly or that wrote this content, newest first."""
        out = {rid: "explicit" for rid in explicit or []}
        for rid in self.by_hash.get(content or "", []):
            out.setdefault(rid, "output hash")
        return sorted(out.items(), key=lambda kv: self.records.get(kv[0], {}).get("started", ""), reverse=True)

    def summary(self, rid: str, how: str) -> dict:
        rec = self.records.get(rid)
        if rec is None:
            return {"id": rid, "link": how, "missing": True}
        keys = ("status", "started", "finished", "exit_code", "commit", "base_commit", "dirty", "command",
                "config_path", "config_hash", "seed", "host", "gpus", "spooler_job", "runner", "data")
        return {"id": rid, "link": how} | {k: rec[k] for k in keys if k in rec}
