"""What environment a run used: Python, platform, GPUs, and every installed package with its version,
and the commit for packages installed from git or as editable checkouts (with +dirty when the
checkout has uncommitted changes).

PROBE runs inside the run's own interpreter (stdlib only, Python >= 3.8) and prints one JSON line.
gpu-spooler embeds a copy to probe a job's environment on the server; keep the two in step.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .hashing import sha256_json

PROBE = r'''
import json, os, platform, subprocess, sys
try:
    from importlib import metadata
except ImportError:
    metadata = None

def git(path, *args):
    try:
        r = subprocess.run(["git", "-C", path] + list(args), capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""

packages = {}
for d in (metadata.distributions() if metadata else []):
    try:
        name = (d.metadata["Name"] or "").lower()
    except Exception:
        continue
    if not name or name in packages:
        continue
    version, commit, dirty = d.version, "", False
    try:
        url = json.loads(d.read_text("direct_url.json") or "null") or {}
    except Exception:
        url = {}
    commit = (url.get("vcs_info") or {}).get("commit_id") or ""
    if (url.get("dir_info") or {}).get("editable") and url.get("url", "").startswith("file://"):
        path = url["url"][len("file://"):]
        commit = git(path, "rev-parse", "HEAD") or commit
        dirty = bool(git(path, "status", "--porcelain", "--untracked-files=no"))
        version += " editable"
    if commit:
        version += " @" + commit[:12] + ("+dirty" if dirty else "")
    packages[name] = version

gpus = []
try:
    out = subprocess.run(["nvidia-smi", "--query-gpu=index,name,driver_version", "--format=csv,noheader"],
                         capture_output=True, text=True, timeout=20).stdout
    gpus = [line.strip() for line in out.splitlines() if line.strip()]
except Exception:
    pass

print(json.dumps({"python": platform.python_version(), "executable": sys.executable,
                  "platform": platform.platform(), "machine": platform.machine(),
                  "cuda_visible_devices": os.environ.get("CUDA_VISIBLE_DEVICES"), "gpus": gpus,
                  "packages": dict(sorted(packages.items()))}, separators=(",", ":")))
'''

PYTHON = re.compile(r"python(\d+(\.\d+)*)?")


def interpreter(argv: list[str]) -> str | None:
    """The Python a command runs under: its first word if that is a python (python, python3.11), else None."""
    if argv and PYTHON.fullmatch(Path(argv[0]).name):
        return shutil.which(argv[0]) or argv[0]
    return None


def finish(env: dict) -> dict:
    """Add env_hash: one value that changes when the Python or any package does."""
    env = dict(env)
    env["hash"] = sha256_json({"python": env.get("python"), "packages": env.get("packages", {})})
    return env


def probe(python: str, cwd: Path | None = None, extra_env: dict | None = None) -> dict | None:
    try:
        r = subprocess.run([python, "-c", PROBE], capture_output=True, text=True, timeout=120, cwd=cwd,
                           env={**os.environ, **(extra_env or {})})
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [line for line in r.stdout.splitlines() if line.startswith("{")]
    if r.returncode != 0 or not lines:
        return None
    try:
        return finish(json.loads(lines[-1]))
    except json.JSONDecodeError:
        return None


def diff(a: dict | None, b: dict | None) -> dict:
    """What differs between two environments."""
    a, b = a or {}, b or {}
    out: dict = {}
    for k in ("python", "platform", "gpus", "executable"):
        if a.get(k) != b.get(k):
            out[k] = (a.get(k), b.get(k))
    pa, pb = a.get("packages", {}), b.get("packages", {})
    changed = {n: (pa.get(n), pb.get(n)) for n in sorted(set(pa) | set(pb)) if pa.get(n) != pb.get(n)}
    if changed:
        out["packages"] = changed
    return out
