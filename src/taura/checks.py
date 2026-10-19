"""`taura check`: verify the paper → artifact → result chain."""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from . import claims, decisions, gitutil
from . import lock as lockmod
from . import paper as papermod
from . import runs as runsmod
from .discovery import LOCK_FILE, Pair
from .errors import TauraError
from .generate import is_bold, leading_number, parse, parse_macros, parse_table
from .hashing import content_hash, sha256_file
from .ledger import ARTIFACTS_FILE, Ledger, base_key, flatten


@dataclass
class Finding:
    id: str
    severity: str  # "error" | "warn"
    repo: str  # "paper" | "code"
    location: str
    message: str
    fix: str
    diff: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if d["diff"] is None:
            del d["diff"]
        return d


# ---- paper-side checks ---------------------------------------------------------


def published_version(paper: Path, rel: str) -> bytes | None:
    """The file as taura last committed it: at the last commit that touched the lock."""
    rev = gitutil.last_commit_touching(paper, LOCK_FILE) if gitutil.is_repo(paper) else None
    return gitutil.show(paper, rev, rel) if rev else None


def _text_diff(old: bytes | None, new: str, rel: str) -> str | None:
    if old is None:
        return None
    lines = difflib.unified_diff(
        old.decode("utf-8", "replace").splitlines(keepends=True),
        new.splitlines(keepends=True),
        fromfile=f"{rel} (published)", tofile=f"{rel} (now)",
    )
    return "".join(lines) or None


def check_hand_edits(paper: Path, lock: dict) -> list[Finding]:
    out = []
    fix = (
        "a collaborator may have corrected this on Overleaf: compare with the diff, move any real "
        "correction into the code repo's results, then restore the file with `git checkout` "
        "(or delete it) and `taura publish`"
    )
    for name, entry in sorted(lock["artifacts"].items()):
        rel = entry["output"]
        f = paper / rel
        if not f.is_file():
            out.append(Finding("hand-edit", "error", "paper", rel, f"generated file for {name} was deleted", fix))
            continue
        if not rel.endswith(".tex"):
            if content_hash(f) != entry["hash"]:
                out.append(Finding("hand-edit", "error", "paper", rel, f"{rel} differs from the published hash", fix))
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        parsed = parse(text)
        if parsed is None:
            msg = f"{rel} has a missing or altered provenance header"
        elif parsed.actual_hash != parsed.declared_hash:
            msg = f"{rel} was edited by hand (body no longer matches its content-hash)"
        elif parsed.actual_hash != entry["hash"]:
            msg = f"{rel} doesn't match the hash in {LOCK_FILE}"
        else:
            continue
        out.append(Finding("hand-edit", "error", "paper", rel, msg, fix, _text_diff(published_version(paper, rel), text, rel)))
    return out


def published_keys(paper: Path) -> dict[str, str]:
    keys: dict[str, str] = {}
    for rel in papermod.generated_files(paper):
        if rel.endswith(".tex"):
            parsed = parse((paper / rel).read_text(encoding="utf-8", errors="replace"))
            if parsed:
                keys.update(parse_macros(parsed.body))
    return keys


def check_dangling_keys(paper: Path, ledger: Ledger | None) -> list[Finding]:
    keys = published_keys(paper)
    code_keys: set[str] = set()
    if ledger is not None:
        try:
            code_keys = {k for name, r in ledger.results.items() for k, *_ in flatten(name, r)}
        except TauraError:
            pass
    out = []
    for use in papermod.res_uses(paper):
        if use.key in keys:
            continue
        if use.key in code_keys:
            result = use.key.split("/")[0]
            fix = f"the key exists in the code repo: add {result!r} to a macros artifact's results in {ARTIFACTS_FILE}, then `taura publish`"
        else:
            close = difflib.get_close_matches(use.key, sorted(set(keys) | code_keys), n=3)
            fix = (f"did you mean {', '.join(close)}? " if close else "") + \
                "otherwise add the result to ledger/results/ and publish"
        out.append(Finding("dangling-key", "error", "paper", use.location,
                           f"\\res{{{use.key}}} is not defined in the published results", fix))
    return out


def check_generated_dir(paper: Path, lock: dict) -> list[Finding]:
    tracked = lockmod.outputs(lock)
    refs = papermod.references(paper)
    out = []
    for rel in papermod.generated_files(paper):
        if rel not in tracked:
            out.append(Finding("untracked-generated", "error", "paper", rel,
                               f"{rel} is in generated/ but not in {LOCK_FILE}",
                               "generated/ is written only by taura: move the file out of generated/, or add an artifact for it"))
        elif not papermod.is_referenced(rel, refs):
            out.append(Finding("unused-artifact", "warn", "paper", rel,
                               f"{rel} is not \\input or \\includegraphics'd anywhere",
                               f"use it in the paper, or remove artifact {tracked[rel]!r} from {ARTIFACTS_FILE}"))
    return out


def _bold_problems(cells: list[tuple[str, str]], rule: str) -> tuple[float, list[str]] | None:
    """cells: (name, cell text). Returns (best, names bolded wrongly) or None if nothing to compare."""
    vals = [(name, leading_number(c), is_bold(c)) for name, c in cells]
    nums = [v for _, v, _ in vals if v is not None]
    if not nums:
        return None
    best = max(nums) if rule == "max" else min(nums)
    return best, [name for name, v, b in vals if v is not None and b != (v == best)]


def check_bold(paper: Path, lock: dict) -> list[Finding]:
    out = []
    for _, entry in sorted(lock["artifacts"].items()):
        rules = entry.get("bold")
        f = paper / entry["output"]
        if not rules or not f.is_file():
            continue
        parsed = parse(f.read_text(encoding="utf-8", errors="replace"))
        if parsed is None:
            continue
        header, rows = parse_table(parsed.body)
        labels = header + [str(j) for j in range(len(header), len(rules))]
        fix = "regenerate with `taura publish`; don't bold by hand"
        if entry.get("bold_by") == "row":
            cols = [j for j, r in enumerate(rules) if r]
            rule = rules[cols[0]]
            for row in rows:
                res = _bold_problems([(labels[j], row[j]) for j in cols if j < len(row)], rule)
                if res and res[1]:
                    out.append(Finding("bold-mismatch", "error", "paper", f"{entry['output']} row {row[0]}",
                                       f"bold in row {row[0]!r} should mark the {rule} ({res[0]:g}); "
                                       f"wrong columns: {', '.join(res[1])}", fix))
            continue
        for j, rule in enumerate(rules):
            if not rule:
                continue
            res = _bold_problems([(r[0] if r else "", r[j]) for r in rows if j < len(r)], rule)
            if res and res[1]:
                out.append(Finding("bold-mismatch", "error", "paper", f"{entry['output']} col {labels[j]}",
                                   f"bold in column {labels[j]!r} should mark the {rule} ({res[0]:g}); "
                                   f"wrong rows: {', '.join(res[1])}", fix))
    return out


def _code_pdfs(code: Path) -> dict[str, list[Path]]:
    """basename -> PDFs in the code repo (hidden dirs and the staging area skipped)."""
    out: dict[str, list[Path]] = {}
    for root, dirs, files in os.walk(code):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.endswith(".pdf"):
                out.setdefault(f, []).append(Path(root) / f)
    return out


def check_figures(paper: Path, code: Path) -> list[Finding]:
    out = []
    pdfs: dict[str, list[Path]] | None = None
    for use, found, ignored in papermod.graphics_uses(paper):
        if found is None:
            out.append(Finding("missing-figure", "error", "paper", use.location,
                               f"\\includegraphics{{{use.key}}}: no such file in the paper repo",
                               "publish the figure artifact, or fix the path"))
            continue
        rel = str(found.relative_to(paper))
        if ignored or rel.startswith(papermod.GENERATED + "/"):
            continue
        if pdfs is None:
            pdfs = _code_pdfs(code)
        same = [p for p in pdfs.get(found.name, []) if content_hash(p) == content_hash(found)]
        if same:
            src = str(same[0].relative_to(code))
            fix = (f"it matches {src} in the code repo (same content): add [artifacts.{found.stem}] "
                   f'kind = "figure", source = "{src}", output = "generated/{found.name}", then publish '
                   f"and point \\includegraphics at generated/{found.name}")
        else:
            fix = "add a figure artifact for it (script or source), or mark the line % taura:ignore"
        out.append(Finding("untracked-figure", "warn", "paper", use.location,
                           f"figure {rel} has no provenance (not generated by taura)", fix))
    return out


def _shown(s: str) -> str:
    """A number as a reader sees it: 4{,}396, 4,396 and 4396 compare equal; 81.2\\% equals 81.2. A
    leading + is dropped: the typed literal never includes it (a typed "+0.0006" is the number 0.0006
    after a sign), while a format with sign = true prints it. Braces only group: 10^{4} is 10^4."""
    return re.sub(r"\{,\}|,|\\%|\$|\s|[{}]", "", s.replace("$-$", "-")).lstrip("+")


def check_prose(paper: Path, ledger: Ledger) -> list[Finding]:
    out = []
    try:
        lits = claims.match(claims.literals(paper), ledger)
        seeds = claims.seed_claims(paper, ledger)
        stored = decisions.load(ledger.code, lits)
        ledger.all_keys()  # surfaces a broken ledger here, not mid-way
    except TauraError:
        return out
    ignore = "; or add % taura:ignore"
    for lit in lits:
        d = stored.get(lit.id)
        if d is not None and d.verdict == "not-a-result":
            continue
        if d is not None and d.verdict == "unrecorded":
            out.append(Finding("unrecorded-result", "warn", "code", lit.location,
                               f"typed number {lit.text} is a computed result the ledger doesn't have ({d.reason})",
                               "add it to ledger/results/ (e.g. in the exporter), then `taura resolve "
                               f"{lit.id} KEY --reason ...`"))
            continue
        if d is not None:  # decided: a result, not yet rewritten
            out.append(Finding("literal-number", "warn", "paper", lit.location,
                               f"typed number {lit.text} is \\res{{{d.key}}} (decided: {d.reason})",
                               f"replace {lit.text} with \\res{{{d.key}}}" + ignore))
            continue
        if not lit.candidates or not lit.strong:
            continue
        if lit.suggestion:
            fix = f"replace {lit.text} with {lit.suggestion}" + (f" and set {lit.format_hint}" if lit.format_hint else "")
        else:
            more = f" (+{lit.more} more)" if lit.more else ""
            fix = (f"decide which of {', '.join(lit.candidates[:claims.MAX_CANDIDATES])}{more} it is: "
                   "`taura adopt --todo`, "
                   f"then `taura resolve {lit.id} KEY --reason ...`")
        out.append(Finding("literal-number", "warn", "paper", lit.location,
                           f"typed number {lit.text} matches a recorded result", fix + ignore))

    # Decisions already applied (the number is now \res{key}): does the key print what the paper said?
    present = {lit.id for lit in lits}
    published = published_keys(paper)
    uses: dict[str, set[str]] = {}
    for use in papermod.res_uses(paper):
        uses.setdefault(use.file, set()).add(use.key)
    for d in stored.values():
        written = [k for k in uses.get(d.file, set()) if base_key(k)[0] == d.key]  # key, or key@N
        if d.verdict != "result" or d.id in present or not written:
            continue
        result = d.key.split("/")[0]
        if d.result_hash and result in ledger.results and ledger.result_hash(result) != d.result_hash:
            continue  # the result changed since the rewrite was verified: a new value is expected
        shown = {k: (published[k], "prints") if k in published else (ledger.render_key(k), "will print")
                 for k in written}
        shown = {k: v for k, v in shown.items() if v[0] is not None}  # dangling-key covers the rest
        if not shown or any(_shown(p) == _shown(d.literal) for p, _ in shown.values()):
            continue
        prints, where = next(iter(shown.values()))
        out.append(Finding("adopt-mismatch", "warn", "paper", f"{d.file} \\res{{{d.key}}}",
                           f"\\res{{{d.key}}} {where} {prints}, but the paper said {d.literal} there",
                           f'set a [format."{d.key}"] entry (or variants = [N] and \\res{{{d.key}@N}}) so it '
                           "prints as before, or confirm the change is intended (a result changed) and clear "
                           f"it with `taura resolve {d.id} --clear`"))
    for c in seeds:
        out.append(Finding("seed-claim", "warn", "paper", c.location,
                           f"text claims {c.claimed} {c.what} but \\res{{{c.key}}} comes from n = {c.actual}",
                           f"write \\res{{{'/'.join(c.key.split('/')[:2])}/n}} instead of the number, or fix the claim"))
    return out


# ---- code-side checks ------------------------------------------------------------


def _rebuilds_identically(ledger: Ledger, art, entry: dict) -> bool:
    """Would building the artifact now give exactly the published content? (provenance-only change)"""
    import tempfile

    from .build import build_one
    try:
        with tempfile.TemporaryDirectory() as tmp:
            return build_one(ledger, art, Path(tmp), "check").hash == entry.get("hash")
    except TauraError:
        return False


def check_stale(ledger: Ledger, lock: dict) -> list[Finding]:
    out = []
    fix = "run `taura diff` to review, then `taura publish`"
    for name, art in ledger.artifacts.items():
        entry = lock["artifacts"].get(name)
        if entry is None:
            out.append(Finding("stale-artifact", "error", "code", name, f"artifact {name} has never been published", fix))
            continue
        reasons = []
        for r in art.results:
            try:
                h = ledger.result_hash(r)
            except TauraError as e:
                out.append(Finding("missing-result", "error", "code", f"{ARTIFACTS_FILE} [artifacts.{name}]", str(e),
                                   f"add ledger/results/{r}.json or drop it from the artifact"))
                reasons = None
                break
            if lock["results"].get(r) != h or r not in entry.get("inputs", []):
                reasons.append(f"result {r} changed")
        if reasons is None:
            continue
        if set(entry.get("inputs", [])) - set(art.results):
            reasons.append("inputs changed")
        if ledger.script_hash(art) != entry.get("script_hash"):
            reasons.append(f"script {art.script} changed")
        if ledger.config_hash(art) != entry.get("config_hash"):
            reasons.append("artifact config or formatting changed")
        if entry.get("output") != art.output:
            reasons.append(f"output moved to {art.output}")
        src = ledger.code / art.source if art.source else None
        if src is not None and src.is_file() and content_hash(src) != entry.get("hash"):
            reasons.append(f"{art.source} changed")
        if reasons and entry.get("output") == art.output and _rebuilds_identically(ledger, art, entry):
            out.append(Finding("provenance-changed", "warn", "code", name,
                               f"{name}'s inputs changed ({'; '.join(reasons)}) but it rebuilds identically: "
                               "the paper doesn't change", "`taura publish` to record the new provenance in the lock"))
        elif reasons:
            out.append(Finding("stale-artifact", "error", "code", name, f"{name} is stale: {'; '.join(reasons)}", fix))
    for name, art in ledger.artifacts.items():
        if not art.source:
            continue
        src = ledger.code / art.source
        if not src.is_file():
            out.append(Finding("missing-source", "error", "code", art.source,
                               f"figure {name}: {art.source} doesn't exist",
                               f"run {art.made_by}" if art.made_by else "regenerate it"))
        elif art.made_by and (ledger.code / art.made_by).stat().st_mtime > src.stat().st_mtime:
            script_hash = sha256_file(ledger.code / art.made_by)
            if art.options.get("checked_against") == script_hash:
                continue  # reviewed: this version of the script doesn't change the figure
            out.append(Finding("stale-source", "warn", "code", art.source,
                               f"figure {name}: {art.source} is older than {art.made_by}",
                               f"re-run {art.made_by}, then `taura publish`; or, if the change doesn't affect "
                               f'this figure, add checked_against = "{script_hash}" to [artifacts.{name}] '
                               "(it holds until the script changes again)"))
    for name in sorted(set(lock["artifacts"]) - set(ledger.artifacts)):
        out.append(Finding("stale-artifact", "error", "code", name,
                           f"{name} is published but no longer in {ARTIFACTS_FILE}", "`taura publish` will remove it"))
    return out


def check_runs(ledger: Ledger, lock: dict) -> list[Finding]:
    """Run provenance: records exist, commits exist, and published results come from clean,
    successful runs whose code hasn't changed since."""
    code = ledger.code
    idx = runsmod.Index(code)
    out = []
    for rid, rec in idx.records.items():
        commit = rec.get("commit")
        if commit and commit != "unknown" and gitutil.is_repo(code) and not runsmod.commit_exists(code, commit):
            out.append(Finding("missing-commit", "error", "code", f"ledger/runs/{rid}.toml",
                               f"run {rid}'s commit {commit[:12]} is not in the code repo",
                               "fetch or restore that commit (was it rebased away?); a snapshot from gpu-spooler "
                               "sync lives under refs/gpu-spooler/ on the server"))
    # what the paper depends on: published results and published figures, with the content they hold
    published: list[tuple[str, list[str] | None, object]] = []
    for name in lock["results"]:
        r = ledger.results.get(name)
        if r is not None:
            published.append((f"result {name}", r.get("runs"), runsmod.source_contents(code, r.get("source"))))
    for name, entry in lock["artifacts"].items():
        if entry.get("kind") == "figure":
            published.append((f"figure {name}", None, entry.get("hash")))
    for name, r in ledger.results.items():
        for rid in r.get("runs") or []:
            if rid not in idx.records:
                out.append(Finding("missing-run", "error", "code", f"ledger/results/{name}.json",
                                   f"result {name} names run {rid}, which has no record in ledger/runs/",
                                   f"record it (`taura record {rid} ...`) or remove it from the result's runs"))
    watch = runsmod.watch_paths(code)
    affected: dict[str, list[str]] = {}
    for what, explicit, content in published:
        # runs named explicitly all count; of the runs that wrote this content, the newest one: an
        # older run whose code has since changed is superseded by a rerun that reproduced it
        links = idx.linked(explicit, content)
        newest = next((rid for rid, how in links if how != "explicit" and rid in idx.records), None)
        for rid, how in links:
            if rid in idx.records and (how == "explicit" or rid == newest):
                affected.setdefault(rid, []).append(what)
    for rid, whats in sorted(affected.items()):
        rec, loc, used = idx.records[rid], f"ledger/runs/{rid}.toml", ", ".join(sorted(set(whats)))
        if rec.get("dirty"):
            out.append(Finding("dirty-run", "warn", "code", loc,
                               f"{used} comes from run {rid}, made with uncommitted changes"
                               + (f" to {', '.join(rec['changed_code'][:5])}" if rec.get("changed_code") else ""),
                               "re-run from a clean, committed tree before submission"))
        if rec.get("exit_code") not in (0, None):
            out.append(Finding("failed-run", "warn", "code", loc,
                               f"{used} comes from run {rid}, which exited {rec['exit_code']}",
                               "check the run; re-run it if its outputs are incomplete"))
        base = rec.get("base_commit") or rec.get("commit")
        changed = runsmod.changed_since(code, base, list(rec.get("inputs") or []) + watch) if base else []
        if changed:
            out.append(Finding("code-changed-since-run", "warn", "code", loc,
                               f"{used} comes from run {rid}, but {', '.join(changed)} changed since its commit "
                               f"{base[:7]}", "re-run it to confirm the results still hold, then publish"))
    return out


# ---- driver ---------------------------------------------------------------------


@dataclass
class Report:
    findings: list[Finding]
    notes: list[str]

    @property
    def errors(self) -> int:
        return sum(f.severity == "error" for f in self.findings)

    @property
    def warnings(self) -> int:
        return sum(f.severity == "warn" for f in self.findings)


def run_checks(pair: Pair) -> Report:
    notes = []
    findings: list[Finding] = []
    ledger = Ledger(pair.code)
    if pair.paper is None:
        notes.append(f"paper repo not found (tried {pair.paper_tried}); ran code-side checks only")
        try:
            _ = ledger.results
        except TauraError as e:
            findings.append(Finding("invalid-result", "error", "code", "ledger/results", str(e), "fix the JSON"))
        return Report(findings, notes)

    lock = lockmod.read(pair.paper)
    findings += check_hand_edits(pair.paper, lock)
    findings += check_bold(pair.paper, lock)
    findings += check_dangling_keys(pair.paper, ledger)
    findings += check_generated_dir(pair.paper, lock)
    findings += check_stale(ledger, lock)
    findings += check_runs(ledger, lock)
    findings += check_figures(pair.paper, pair.code)
    findings += check_prose(pair.paper, ledger)
    order = {"error": 0, "warn": 1}
    findings.sort(key=lambda f: (order[f.severity], f.id, f.location))
    return Report(findings, notes)
