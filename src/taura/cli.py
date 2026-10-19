"""Command-line interface. Exit codes: 0 clean, 1 findings/refusal, 2 usage or config error."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import NAME, __version__, claims, decisions
from . import lock as lockmod
from . import trace as tracemod
from .build import build
from .checks import run_checks
from .discovery import Pair, discover
from .errors import Refusal, TauraError
from .ledger import Ledger
from .model import Project
from .papers import all_pairs
from .publish import diff, publish
from .scaffold import init


def _emit(args, data, text: str) -> None:
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    elif text:
        print(text)


def _pair(args) -> Pair:
    return discover(Path(args.C).expanduser() if args.C else None, args.paper)


# ---- commands --------------------------------------------------------------------


def cmd_init(args) -> int:
    r = init(_pair(args))
    lines = [f"code:  {r['code']}"]
    lines.append(f"paper: {r['paper']}" if r["paper"] else f"paper: not found (tried {r['paper_tried']}); skipped generated/")
    for k in ("created", "updated", "skipped"):
        if r[k]:
            lines.append(f"{k}: {', '.join(r[k])}")
    if r["paper_created"]:
        lines.append(f"created in paper: {', '.join(r['paper_created'])}")
    if r["instructions"]:
        lines += ["", r["instructions"]]
    _emit(args, r, "\n".join(lines))
    return 0


def cmd_build(args) -> int:
    pair = _pair(args)
    built = build(Ledger(pair.code), args.artifacts or None)
    data = [{"artifact": b.artifact.name, "output": b.artifact.output, "staged": str(b.path), "hash": b.hash} for b in built]
    _emit(args, {"built": data}, "\n".join(f"built {d['artifact']} -> {d['staged']}" for d in data) or "nothing to build")
    return 0


def cmd_diff(args) -> int:
    changes = diff(_pair(args))
    _emit(args, {"changes": [asdict(c) | {"text": str(c)} for c in changes]},
          "\n".join(str(c) for c in changes) or "no changes")
    return 1 if changes else 0


def cmd_publish(args) -> int:
    r = publish(_pair(args), allow_dirty_code=args.allow_dirty_code, fetch=not args.no_fetch, dry_run=args.dry_run)
    lines = [c["text"] for c in r["changes"]]
    if not lines:
        text = "nothing to publish"
    elif r["dry_run"]:
        text = "would publish:\n" + "\n".join(lines)
    else:
        text = "\n".join(lines) + f"\ncommitted {r['commit'][:7]} in the paper repo (not pushed)"
    _emit(args, r, text)
    return 0


def _format_finding(f) -> str:
    out = [f"{f.severity:<5}  {f.id}  {f.repo}:{f.location}", f"       {f.message}", f"       fix: {f.fix}"]
    if f.diff:
        out += ["       " + l for l in f.diff.rstrip("\n").splitlines()]
    return "\n".join(out)


def cmd_check(args) -> int:
    report = run_checks(_pair(args))
    data = {"findings": [f.to_dict() for f in report.findings], "errors": report.errors,
            "warnings": report.warnings, "notes": report.notes}
    lines = [f"note: {n}" for n in report.notes] + [_format_finding(f) for f in report.findings]
    lines.append(f"{report.errors} error(s), {report.warnings} warning(s)" if report.findings else "ok: no findings")
    _emit(args, data, "\n".join(lines))
    return 1 if report.findings else 0


def cmd_status(args) -> int:
    pair = _pair(args)
    report = run_checks(pair)
    lock = lockmod.read(pair.paper) if pair.paper else {"artifacts": {}}
    counts: dict[str, int] = {}
    for f in report.findings:
        counts[f.id] = counts.get(f.id, 0) + 1
    data = {
        "code": str(pair.code),
        "paper": str(pair.paper) if pair.paper else None,
        "paper_tried": str(pair.paper_tried),
        "last_publish": lock.get("published"),
        "published_commit": lock.get("code_commit"),
        "artifacts": len(Ledger(pair.code).artifacts),
        "published_artifacts": len(lock["artifacts"]),
        "findings": counts,
    }
    lines = [
        f"code:   {data['code']}",
        f"paper:  {data['paper'] or 'not found (tried ' + data['paper_tried'] + ')'}",
        f"last publish: {data['last_publish'] or 'never'}"
        + (f" from {data['published_commit'][:7]}" if data["published_commit"] else ""),
        f"artifacts: {data['artifacts']} defined, {data['published_artifacts']} published",
        "findings: " + (", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "none"),
    ]
    _emit(args, data, "\n".join(lines))
    return 1 if counts else 0


def _adopt_line(l, d) -> str:
    loc = f"{l.location:<22} {l.id:<12} {l.text:<9}"
    if d is not None:
        what = f"\\res{{{d.key}}}" if d.verdict == "result" else d.verdict
        return f"{loc} ✓ {what}  ({d.by or 'by hand'}: {d.reason})"
    if l.suggestion:
        note = f"  (prints {l.renders_as}; set {l.format_hint})" if l.format_hint else ""
        weak = "" if l.strong else "  [weak: few digits, could be a coincidence]"
        return f"{loc} → {l.suggestion}{note}{weak}"
    if l.candidates:
        more = f" (+{l.more} more)" if l.more else ""
        return f"{loc} ? {' | '.join(l.candidates[:claims.MAX_CANDIDATES])}{more}"
    return f"{loc}   no matching result: {l.context}"


def cmd_adopt(args) -> int:
    pair = _pair(args)
    lits = claims.match(claims.literals(pair.require_paper()), Ledger(pair.code))
    decided = decisions.apply_to(lits, decisions.load(pair.code))
    problems: list[str] = []

    if args.agent:
        todo = [l for l in lits if l.id not in decided and decisions.needs_agent(l, args.include_unique)]
        todo = todo[:args.limit] if args.limit else todo
        log = (lambda m: None) if args.json else (lambda m: print(m, file=sys.stderr))
        new, problems = decisions.ask_agent(args.agent, todo, batch=args.batch, log=log)
        if new:
            stored = decisions.load(pair.code)
            stored.update({d.id: d for d in new})
            decisions.save(pair.code, stored)
            decided = decisions.apply_to(lits, stored)

    if args.todo:
        shown = [l for l in lits if l.id not in decided and decisions.needs_agent(l, args.include_unique)]
    elif args.all:
        shown = lits
    else:
        shown = [l for l in lits if l.candidates or l.id in decided]

    verdicts = {v: sum(d.verdict == v for d in decided.values()) for v in decisions.VERDICTS}
    pending = [l for l in lits if l.id not in decided]
    summary = {
        "literals": len(lits),
        "unique": sum(bool(l.suggestion) for l in pending),
        "ambiguous": sum(len(l.candidates) > 1 for l in pending),
        "unmatched": sum(not l.candidates for l in pending),
        "decided": verdicts,
        "needs_decision": sum(decisions.needs_agent(l, False) for l in pending),
    }
    lines = [_adopt_line(l, decided.get(l.id)) for l in shown]
    lines.append(
        f"{summary['literals']} typed numbers. Undecided: {summary['unique']} match one result, "
        f"{summary['ambiguous']} ambiguous, {summary['unmatched']} unmatched. Decided: "
        + ", ".join(f"{n} {v}" for v, n in verdicts.items()))
    if not args.all and not args.todo:
        lines.append("--todo lists what needs a decision (with ids); --all lists everything")
    lines.append("report only: taura never edits the .tex; replace numbers with \\res{key} yourself")
    lines += [f"problem: {p}" for p in problems]
    data = {"literals": [l.to_dict() | {"decision": decided[l.id].__dict__ if l.id in decided else None}
                         for l in shown],
            "summary": summary, "problems": problems}
    _emit(args, data, "\n".join(lines))
    return 1 if problems or summary["unique"] or summary["ambiguous"] else 0


def cmd_resolve(args) -> int:
    pair = _pair(args)
    led = Ledger(pair.code)
    lits = claims.match(claims.literals(pair.require_paper()), led)
    stored = decisions.load(pair.code)
    by = args.by or ""
    changed = []
    if args.accept_unique:
        for l in lits:
            if l.suggestion and l.strong and l.id not in stored:
                d = decisions.make(l, "result", l.candidates[0], "the only recorded result with this value", by or NAME)
                stored[l.id] = d
                changed.append(d)
    else:
        if not args.id:
            raise TauraError("give a literal id (from `taura adopt --todo`), or --accept-unique")
        lit = next((l for l in lits if l.id == args.id), None)
        if lit is None:
            raise TauraError(f"no typed number with id {args.id} in the paper (ids come from `taura adopt`)")
        if args.clear:
            stored.pop(lit.id, None)
        else:
            verdict = "not-a-result" if args.none else "unrecorded" if args.unrecorded else "result"
            if verdict == "result" and not args.key:
                raise TauraError("give the result KEY, or --none / --unrecorded")
            d = decisions.make(lit, verdict, args.key or "", args.reason or "", by)
            stored[lit.id] = d
            changed.append(d)
    decisions.save(pair.code, stored)
    lines = [f"{d.id} {d.literal} → " + (f"\\res{{{d.key}}}" if d.verdict == "result" else d.verdict) for d in changed]
    if args.clear:
        lines.append(f"cleared {args.id}")
    lines.append(f"recorded in {decisions.DECISIONS_FILE}")
    _emit(args, {"decided": [d.__dict__ for d in changed], "file": decisions.DECISIONS_FILE}, "\n".join(lines))
    return 0


def cmd_trace(args) -> int:
    t = tracemod.trace(Project(_pair(args)), args.target)
    _emit(args, t, tracemod.render(t))
    return 0


def cmd_papers(args) -> int:
    rows = [Project(p).summary() for p in all_pairs()]
    lines = []
    for r in rows:
        state = f"{r['errors']} error(s), {r['warnings']} warning(s)" if r["errors"] or r["warnings"] else "clean"
        pub = (r["last_publish"] or "never")[:16].replace("T", " ")
        lines.append(f"{r['name']:<24} {r['claims']:>4} claims  {r['artifacts']:>3} artifacts  "
                     f"published {pub:<16}  {state}")
    _emit(args, {"papers": rows}, "\n".join(lines) or "no paired repos found under the roots")
    return 1 if any(r["errors"] for r in rows) else 0


def cmd_tui(args) -> int:
    try:
        from .tui import run
    except ImportError as e:
        raise TauraError(f"the TUI needs Textual: pip install 'taura[tui]' ({e})") from None
    pair = None
    if not args.all:
        try:
            pair = _pair(args)
        except TauraError:
            pair = None  # not inside a repo: start from the list of papers
    return run(pair)


# ---- parser ----------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="machine-readable output")
    common.add_argument("--paper", default=argparse.SUPPRESS, metavar="NAME",
                        help="which paper, when taura.toml lists several")
    common.add_argument("-C", default=argparse.SUPPRESS, metavar="DIR", help="run as if started in DIR")

    p = argparse.ArgumentParser(prog=NAME, parents=[common],
                                description="Provenance ledger linking paper numbers to code, configs and runs.")
    p.add_argument("--version", action="version", version=f"{NAME} {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    s = sub.add_parser("init", parents=[common], help="scaffold ledger/, artifacts/ and AGENTS.md (idempotent)")
    s.set_defaults(func=cmd_init)
    s = sub.add_parser("build", parents=[common], help="build artifacts into the code repo's staging area")
    s.add_argument("artifacts", nargs="*", help="artifact names (default: all)")
    s.set_defaults(func=cmd_build)
    s = sub.add_parser("diff", parents=[common], help="value-level preview of what publish would change (exit 1 if any)")
    s.set_defaults(func=cmd_diff)
    s = sub.add_parser("publish", parents=[common], help="build, copy into the paper's generated/, update the lock, commit (never pushes)")
    s.add_argument("--allow-dirty-code", action="store_true", help="publish from a code tree with uncommitted changes")
    s.add_argument("--no-fetch", action="store_true", help="don't `git fetch` the paper repo to check it's up to date")
    s.add_argument("--dry-run", action="store_true", help="run the preflight checks and show changes; write nothing")
    s.set_defaults(func=cmd_publish)
    s = sub.add_parser("check", parents=[common], help="check the provenance chain (exit 1 on any finding)")
    s.set_defaults(func=cmd_check)
    s = sub.add_parser("status", parents=[common], help="paired repos, last publish, finding counts")
    s.set_defaults(func=cmd_status)
    s = sub.add_parser("adopt", parents=[common],
                       help="list numbers typed into the paper and the result keys they match (report only)")
    s.add_argument("--all", action="store_true", help="also list numbers that match no result")
    s.add_argument("--todo", action="store_true",
                   help="only undecided numbers that need judgement: ties, weak matches, unmatched")
    s.add_argument("--agent", metavar="CMD",
                   help='ask an agent CLI to decide the --todo numbers from context, e.g. --agent "claude -p" '
                        "(prompt on stdin, JSON reply on stdout); decisions go to ledger/adopt.toml")
    s.add_argument("--batch", type=int, default=20, help="numbers per agent call (default 20)")
    s.add_argument("--limit", type=int, metavar="N", help="with --agent, ask about at most N numbers this run")
    s.add_argument("--include-unique", action="store_true",
                   help="with --todo/--agent, also include numbers with one strong match")
    s.set_defaults(func=cmd_adopt)
    s = sub.add_parser("resolve", parents=[common],
                       help="record what a typed number is: a result key, --none, or --unrecorded")
    s.add_argument("id", nargs="?", help="literal id from `taura adopt`")
    s.add_argument("key", nargs="?", help="the result key it reports")
    s.add_argument("--none", action="store_true", help="not a result (year, setting, sample size...)")
    s.add_argument("--unrecorded", action="store_true", help="a computed result missing from the ledger")
    s.add_argument("--reason", help="one sentence: what in the text decided it (required)")
    s.add_argument("--by", help="who decided (an agent's name, or yours)")
    s.add_argument("--clear", action="store_true", help="forget the decision for this id")
    s.add_argument("--accept-unique", action="store_true",
                   help="record every undecided number with exactly one strong match as that result")
    s.set_defaults(func=cmd_resolve)
    s = sub.add_parser("trace", parents=[common],
                       help="walk a claim back: a \\res key, file.tex:LINE, or an artifact name")
    s.add_argument("target", help="mba/f1/mean | res:mba/f1/mean | main.tex:142 | table_main")
    s.set_defaults(func=cmd_trace)
    s = sub.add_parser("papers", parents=[common], help="every paired code and paper repo under the roots")
    s.set_defaults(func=cmd_papers)
    s = sub.add_parser("tui", parents=[common],
                       help="browse papers, claims and their provenance (read-only; needs taura[tui])")
    s.add_argument("--all", action="store_true", help="start from the list of papers even inside a repo")
    s.set_defaults(func=cmd_tui)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    for k, v in (("json", False), ("paper", None), ("C", None)):
        if not hasattr(args, k):
            setattr(args, k, v)
    try:
        return args.func(args)
    except (TauraError, Refusal) as e:
        if args.json:
            print(json.dumps({"error": str(e), "exit_code": e.exit_code}, indent=2))
        else:
            print(f"{NAME}: {e}", file=sys.stderr)
        return e.exit_code
