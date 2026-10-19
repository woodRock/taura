"""Timings (how long scripts take) as ledger results, from the run records in ledger/runs/.

Declared per repo in taura.toml:

    [timings.evaluation]
    scripts = ["scripts/01_align.py", "scripts/03_benchmark.py"]   # summed

Each becomes a result named timing-<name>: minutes and seconds (the sum, over the scripts, of the
newest successful run of each), and the runs it was taken from, so a rerun updates the paper's
"takes about 4 min" like any other result. A script with no successful run gives no metrics
(\\res{} keys for it are then dangling, which `taura check` reports).
"""

from __future__ import annotations

import shlex
from datetime import datetime
from pathlib import Path

from . import tomlio
from .discovery import CONFIG_FILE
from .errors import TauraError
from .runs import load_all

PREFIX = "timing-"


def declared(code: Path) -> dict[str, dict]:
    cfg_path = code / CONFIG_FILE
    cfg = tomlio.load(cfg_path) if cfg_path.is_file() else {}
    out = {}
    for name, spec in cfg.get("timings", {}).items():
        scripts = spec.get("scripts") if isinstance(spec, dict) else None
        if not scripts or not isinstance(scripts, list) or not all(isinstance(s, str) for s in scripts):
            raise TauraError(f'{cfg_path}: [timings.{name}] needs scripts = ["path/to/script.py", ...]')
        out[name] = spec
    return out


def _runs_script(rec: dict, script: str) -> bool:
    try:
        argv = shlex.split(rec.get("command", ""))
    except ValueError:
        return False
    return any(a == script or a.endswith("/" + script) for a in argv)


def _seconds(rec: dict) -> float | None:
    try:
        return (datetime.fromisoformat(rec["finished"]) - datetime.fromisoformat(rec["started"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        return None


def newest_run(records: dict[str, dict], script: str) -> tuple[str, float] | None:
    """(run id, seconds) of the newest successful run of a script."""
    done = [(rec.get("started", ""), rid, s) for rid, rec in records.items()
            if rec.get("status") == "done" and rec.get("exit_code") == 0 and _runs_script(rec, script)
            and (s := _seconds(rec)) is not None]
    if not done:
        return None
    _, rid, s = max(done)
    return rid, s


def load(code: Path) -> dict[str, dict]:
    """{timing-<name>: result} for every [timings.<name>] in taura.toml."""
    specs = declared(code)
    if not specs:
        return {}
    records = load_all(code)
    out = {}
    for name, spec in specs.items():
        found = {s: newest_run(records, s) for s in spec["scripts"]}
        runs = [f[0] for f in found.values() if f]
        complete = all(found.values())
        seconds = sum(f[1] for f in found.values() if f)
        metrics = {"minutes": round(seconds / 60, 2), "seconds": round(seconds, 1)} if complete else {}
        missing = [s for s, f in found.items() if not f]
        out[PREFIX + name] = {
            "name": PREFIX + name, "kind": "timing",
            "label": f"Run time of {', '.join(spec['scripts'])}"
                     + (f" (no successful run of {', '.join(missing)})" if missing else ""),
            "metrics": metrics, "runs": runs,
        }
    return out
