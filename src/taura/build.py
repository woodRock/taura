"""Build artifacts into a staging area in the code repo (<code>/.taura/build/).

`build` never touches the paper repo; `publish` copies from staging.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import NAME, gitutil
from .discovery import contract
from .errors import TauraError
from .generate import parse, render_header, render_macros, render_table
from .hashing import content_hash, sha256_text
from .ledger import ROUNDINGS, Artifact, Ledger, flatten

STAGING = Path(gitutil.STAGING_DIR) / "build"
ENV_PYTHON = f"{NAME.upper()}_PYTHON"


@dataclass
class Built:
    artifact: Artifact
    path: Path  # staged file
    hash: str  # body content-hash (text) or file hash (binary)
    inputs: dict[str, str]  # result name -> result hash
    script_hash: str | None
    config_hash: str
    bold: list[str] = field(default_factory=list)
    bold_by: str = "column"

    def lock_entry(self) -> dict:
        entry = {
            "kind": self.artifact.kind,
            "output": self.artifact.output,
            "hash": self.hash,
            "inputs": sorted(self.inputs),
            "config_hash": self.config_hash,
        }
        if self.artifact.source:
            entry["source"] = self.artifact.source
        if self.script_hash:
            entry["script"] = self.artifact.script or self.artifact.made_by
            entry["script_hash"] = self.script_hash
        if any(self.bold):
            entry["bold"] = self.bold
            if self.bold_by != "column":
                entry["bold_by"] = self.bold_by
        return entry


def code_commit(code: Path) -> tuple[str, str]:
    """(full sha, short label for headers, with -dirty when the tree has changes)."""
    sha = gitutil.head(code) if gitutil.is_repo(code) else None
    if sha is None:
        return "unknown", "unknown"
    short = sha[:7] + ("-dirty" if gitutil.is_dirty(code) else "")
    return sha, short


def _python(ledger: Ledger) -> str:
    configured = ledger.config.get("build", {}).get("python") or os.environ.get(ENV_PYTHON)
    if configured:
        return configured
    return shutil.which("python3") or sys.executable


def _run_script(ledger: Ledger, art: Artifact, out: Path) -> None:
    results = {r: ledger.result(r) for r in art.results}
    with tempfile.TemporaryDirectory(prefix=f"{NAME}-") as tmp:
        rpath = Path(tmp) / "results.json"
        rpath.write_text(json.dumps(results, indent=2, sort_keys=True))
        env = {
            **os.environ,
            f"{NAME.upper()}_ARTIFACT": art.name,
            f"{NAME.upper()}_RESULTS": str(rpath),
            f"{NAME.upper()}_OUTPUT": str(out),
            f"{NAME.upper()}_OPTIONS": json.dumps(art.options, sort_keys=True),
            # Reproducible output from tools that honour it (matplotlib PDF dates, etc).
            "SOURCE_DATE_EPOCH": "0",
            "PYTHONHASHSEED": "0",
        }
        proc = subprocess.run(
            [_python(ledger), str(ledger.code / art.script)],
            cwd=ledger.code, env=env, capture_output=True, text=True,
        )
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-15:])
        raise TauraError(f"artifact {art.name}: {art.script} exited {proc.returncode}\n{tail}")
    if not out.is_file():
        raise TauraError(f"artifact {art.name}: {art.script} did not write {NAME.upper()}_OUTPUT ({out})")


def _table_spec(ledger: Ledger, art: Artifact, tmp: Path) -> dict:
    if art.script:
        spec_path = tmp / f"{art.name}.json"
        _run_script(ledger, art, spec_path)
        try:
            return json.loads(spec_path.read_text())
        except json.JSONDecodeError as e:
            raise TauraError(f"artifact {art.name}: script output is not a JSON table spec: {e}") from None

    columns = art.options.get("columns")
    if not columns:
        raise TauraError(f"artifact {art.name}: a table needs a script or `columns = [...]`")
    labels = art.options.get("labels", {})
    spec_cols = [{"label": art.options.get("row_header", "Method")}]
    for c in columns:
        if "metric" not in c:
            raise TauraError(f"artifact {art.name}: each column needs a `metric`")
        col = {"label": c.get("label", c["metric"]), "metric": c["metric"]}
        if "bold" in c:
            col["bold"] = c["bold"]
        spec_cols.append(col)
    rows = []
    for r in art.results:
        metrics = ledger.result(r)["metrics"]
        label = labels.get(r) or ledger.result(r).get("label") or r.replace("_", r"\_")
        row = [label]
        for c in columns:
            v = metrics.get(c["metric"])
            if isinstance(v, dict):
                v = {k: v[k] for k in ("mean", "std") if k in v} if "mean" in v else None
            row.append(v)
        rows.append(row)
    return {"columns": spec_cols, "rows": rows}


def build_one(ledger: Ledger, art: Artifact, staging: Path, commit_label: str) -> Built:
    out = staging / art.output
    out.parent.mkdir(parents=True, exist_ok=True)
    inputs = {r: ledger.result_hash(r) for r in art.results}
    bold: list[str] = []
    bold_by = "column"

    if art.kind == "figure":
        if out.exists():
            out.unlink()
        if art.source:
            src = ledger.code / art.source
            if not src.is_file():
                hint = f" (run {art.made_by})" if art.made_by else ""
                raise TauraError(f"artifact {art.name}: source {art.source} not found{hint}")
            shutil.copyfile(src, out)
        else:
            _run_script(ledger, art, out)
        digest = content_hash(out)
    else:
        if art.kind == "macros":
            values = []
            for r in art.results:
                for key, metric, stat, v in flatten(r, ledger.result(r)):
                    fmt = ledger.format_for(r, metric, key)
                    values.append((key, fmt.render(v, stat)))
                    for d in fmt.variants:  # rounded restatements key@N, and bounds key@up / key@down
                        if not (isinstance(d, int) or d in ROUNDINGS):
                            raise TauraError(f"format for {key}: variants are decimals (2) or "
                                             f"{' / '.join(ROUNDINGS)}, got {d!r}")
                        values.append((f"{key}@{d}", ledger.render_key(f"{key}@{d}")))
            body = render_macros(sorted(values))
        else:
            with tempfile.TemporaryDirectory(prefix=f"{NAME}-") as tmp:
                spec = _table_spec(ledger, art, Path(tmp))
            # Per-column formats come from the column's metric (shared across results).
            options = {**art.options, "bold": art.bold} if art.bold else art.options
            body, bold, bold_by = render_table(spec, lambda m: ledger.format_for(None, m), options)
        header = render_header(art.name, commit_label, contract(ledger.code), inputs, body)
        out.write_bytes((header + body).encode("utf-8"))
        digest = sha256_text(body)

    return Built(art, out, digest, inputs, ledger.script_hash(art), ledger.config_hash(art), bold, bold_by)


def build(ledger: Ledger, names: list[str] | None = None) -> list[Built]:
    staging = ledger.code / STAGING
    _, label = code_commit(ledger.code)
    built = [build_one(ledger, a, staging, label) for a in ledger.select(names)]
    if not names:  # a full build drops staged files of removed artifacts
        keep = {b.path for b in built}
        for f in staging.rglob("*") if staging.is_dir() else []:
            if f.is_file() and f not in keep:
                f.unlink()
    return built


def body_of(path: Path) -> str | None:
    """Body of a generated text file, or None if it has no valid header."""
    p = parse(path.read_text(encoding="utf-8"))
    return p.body if p else None
