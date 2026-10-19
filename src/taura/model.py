"""One paired code and paper repo, as data: what `trace`, `papers` and the TUI read.

Everything here is derived from the files (lock, ledger, .tex) on demand and cached for the
lifetime of the object; nothing is written. Build a new Project to see changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from . import claims as claimsmod
from . import decisions as decmod
from . import lock as lockmod
from . import paper as papermod
from . import runs as runsmod
from .checks import Finding, Report, published_keys, run_checks
from .discovery import Pair, contract
from .errors import TauraError
from .ledger import RESULTS_DIR, Ledger


@dataclass
class Claim:
    """A \\res{key} in the paper."""

    file: str
    line: int
    key: str
    printed: str | None  # what the published macros print (None: not published -> ??key??)
    current: str | None  # what the code repo's results would print now (None: no such key)
    status: str  # ok | stale (printed != current) | dangling (not published) | unknown (no result)
    text: str = ""  # the source line

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}"

    @property
    def result(self) -> str:
        return self.key.split("/")[0]


@dataclass
class ArtifactView:
    name: str
    kind: str
    output: str
    used_at: list[str] = field(default_factory=list)  # file:line where the paper \input's / includes it
    published: bool = False
    status: str = "ok"  # ok, or the ids of findings about it (stale-artifact, hand-edit, ...)


@dataclass
class Typed:
    """A number typed into the paper, with what it was decided to be."""

    literal: claimsmod.Literal
    decision: decmod.Decision | None

    @property
    def verdict(self) -> str:
        if self.decision:
            return self.decision.verdict
        if len(self.literal.candidates) > 1:
            return "ambiguous"
        return "matches" if self.literal.candidates else "unmatched"


class Project:
    def __init__(self, pair: Pair):
        self.pair = pair
        self._line_cache: dict[str, list[str]] = {}

    # ---- basics -------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self.pair.paper_name or (self.pair.paper or self.pair.paper_tried).name

    @property
    def code(self) -> Path:
        return self.pair.code

    @property
    def paper(self) -> Path | None:
        return self.pair.paper

    @cached_property
    def ledger(self) -> Ledger | None:
        try:
            return Ledger(self.code)
        except TauraError:
            return None

    @cached_property
    def lock(self) -> dict:
        return lockmod.read(self.paper) if self.paper else {"results": {}, "artifacts": {}}

    @cached_property
    def published(self) -> dict[str, str]:
        return published_keys(self.paper) if self.paper else {}

    @cached_property
    def keys(self) -> dict[str, tuple]:
        try:
            return self.ledger.all_keys() if self.ledger else {}
        except TauraError:
            return {}

    def render(self, key: str) -> str | None:
        """What \\res{key} would print from the code repo's results now (key@N: with N decimals)."""
        try:
            return self.ledger.render_key(key) if self.ledger is not None else None
        except TauraError:
            return None

    @cached_property
    def runs(self) -> runsmod.Index:
        return runsmod.Index(self.code)

    def runs_for(self, explicit: list[str] | None, contents) -> list[dict]:
        return [self.runs.summary(rid, how) for rid, how in self.runs.linked(explicit, contents)]

    @cached_property
    def report(self) -> Report:
        try:
            return run_checks(self.pair)
        except TauraError as e:
            return Report([Finding("config", "error", "code", str(self.code), str(e), "fix the configuration")], [])

    @property
    def findings(self) -> list[Finding]:
        return self.report.findings

    def _lines(self, rel: str) -> list[str]:
        if rel not in self._line_cache:
            path = self.paper / rel if self.paper else None
            text = path.read_text(encoding="utf-8", errors="replace") if path and path.is_file() else ""
            self._line_cache[rel] = text.splitlines()
        return self._line_cache[rel]

    def line_text(self, rel: str, line: int) -> str:
        lines = self._lines(rel)
        return lines[line - 1].strip() if 0 < line <= len(lines) else ""

    # ---- what the paper contains -------------------------------------------------------

    @cached_property
    def claims(self) -> list[Claim]:
        if not self.paper:
            return []
        out = []
        for use in papermod.res_uses(self.paper):
            printed, current = self.published.get(use.key), self.render(use.key)
            if printed is None:
                status = "dangling"
            elif current is None:
                status = "unknown"
            else:
                status = "ok" if printed == current else "stale"
            out.append(Claim(use.file, use.line, use.key, printed, current, status, self.line_text(use.file, use.line)))
        return out

    @cached_property
    def artifacts(self) -> list[ArtifactView]:
        names = set(self.lock["artifacts"]) | (set(self.ledger.artifacts) if self.ledger else set())
        uses: dict[str, list[str]] = {}
        if self.paper:
            for rel, i, line in papermod.raw_lines(self.paper):
                for m in papermod.REFERENCE.finditer(papermod.strip_comment(line)):
                    uses.setdefault(papermod._norm(m.group(1)), []).append(f"{rel}:{i}")
        about: dict[str, set[str]] = {}
        for f in self.findings:
            for name in names:
                entry = self.lock["artifacts"].get(name, {})
                art = self.ledger.artifacts.get(name) if self.ledger else None
                output = entry.get("output") or (art.output if art else "")
                if f.location == name or (output and f.location.startswith(output)):
                    about.setdefault(name, set()).add(f.id)
        out = []
        for name in sorted(names):
            art = self.ledger.artifacts.get(name) if self.ledger else None
            entry = self.lock["artifacts"].get(name)
            output = art.output if art else entry["output"]
            kind = art.kind if art else entry.get("kind", "?")
            status = ", ".join(sorted(about.get(name, ()))) or "ok"
            out.append(ArtifactView(name, kind, output, uses.get(papermod._norm(output), []), entry is not None, status))
        return out

    @cached_property
    def typed(self) -> list[Typed]:
        if not self.paper or self.ledger is None:
            return []
        try:
            lits = claimsmod.match(claimsmod.literals(self.paper), self.ledger)
            stored = decmod.load(self.code)
        except TauraError:
            return []
        return [Typed(lit, stored.get(lit.id)) for lit in lits]

    # ---- provenance pieces ---------------------------------------------------------------

    def result_info(self, name: str, metric: str | None = None, stat: str | None = None) -> dict:
        """One result: where it lives, its hash now and when published, where it came from."""
        info: dict = {"name": name, "file": f"{RESULTS_DIR}/{name}.json"}
        r = self.ledger.results.get(name) if self.ledger else None
        if r is None:
            info["missing"] = True
            return info
        info["label"] = r.get("label", name)
        info["hash"] = self.ledger.result_hash(name)
        info["published_hash"] = self.lock["results"].get(name)
        info["changed_since_publish"] = info["published_hash"] not in (None, info["hash"])
        for k in ("produced_by", "created", "runs", "source"):
            if k in r:
                info[k] = r[k]
        info["run_records"] = self.runs_for(r.get("runs"), runsmod.source_contents(self.code, r.get("source")))
        if metric is not None:
            v = r["metrics"].get(metric)
            info["key"] = "/".join(x for x in (name, metric, stat) if x)
            info["value"] = v.get(stat) if isinstance(v, dict) and stat else v
        return info

    @property
    def publish_info(self) -> dict:
        lock = self.lock
        return {k: lock.get(k) for k in ("code_repo", "code_commit", "published")} | {
            "paper": contract(self.paper) if self.paper else None}

    def produced_by_run(self, rid: str) -> list[str]:
        """What the paper uses that a run produced: results (by explicit link or output hash) and figures."""
        out = []
        if self.ledger is not None:
            for name, r in self.ledger.results.items():
                contents = runsmod.source_contents(self.code, r.get("source"))
                if any(x == rid for x, _ in self.runs.linked(r.get("runs"), contents)):
                    out.append(f"result {name}" + ("" if name in self.lock["results"] else " (not published)"))
        for name, entry in self.lock["artifacts"].items():
            if entry.get("kind") == "figure" and any(x == rid for x, _ in self.runs.linked(None, entry.get("hash"))):
                out.append(f"figure {name}")
        return sorted(out)

    def summary(self) -> dict:
        claims = self.claims
        typed = self.typed
        return {
            "name": self.name,
            "code": str(self.code),
            "paper": str(self.paper) if self.paper else None,
            "last_publish": self.lock.get("published"),
            "published_commit": self.lock.get("code_commit"),
            "claims": len(claims),
            "claims_stale": sum(c.status == "stale" for c in claims),
            "claims_dangling": sum(c.status == "dangling" for c in claims),
            "artifacts": len(self.artifacts),
            "typed_numbers": len(typed),
            "typed_undecided": sum(t.decision is None for t in typed),
            "runs": len(self.runs.records),
            "errors": self.report.errors,
            "warnings": self.report.warnings,
        }
