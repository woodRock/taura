"""Command-line interface. Exit codes: 0 clean, 1 findings/refusal, 2 usage or config error."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from . import NAME, __version__, claims
from . import lock as lockmod
from .build import build
from .checks import run_checks
from .discovery import Pair, discover
from .errors import Refusal, TauraError
from .ledger import Ledger
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


def cmd_adopt(args) -> int:
    pair = _pair(args)
    lits = claims.match(claims.literals(pair.require_paper()), Ledger(pair.code))
    shown = lits if args.all else [l for l in lits if l.candidates]
    unique = sum(bool(l.suggestion) for l in lits)
    ambiguous = sum(len(l.candidates) > 1 for l in lits)
    summary = {"literals": len(lits), "unique": unique, "ambiguous": ambiguous,
               "unmatched": len(lits) - unique - ambiguous}
    lines = []
    for l in shown:
        if l.suggestion:
            note = f"  (prints {l.renders_as}; set {l.format_hint})" if l.format_hint else ""
            weak = "" if l.strong else "  [weak: few digits, could be a coincidence]"
            lines.append(f"{l.location:<24} {l.text:<10} → {l.suggestion}{note}{weak}")
        elif l.candidates:
            more = f" (+{l.more} more)" if l.more else ""
            lines.append(f"{l.location:<24} {l.text:<10} ? {' | '.join(l.candidates)}{more}")
        else:
            lines.append(f"{l.location:<24} {l.text:<10}   no matching result: {l.context}")
    lines.append(f"{summary['literals']} typed numbers: {unique} match one result, {ambiguous} ambiguous, "
                 f"{summary['unmatched']} unmatched" + ("" if args.all else " (--all lists them)"))
    lines.append("report only: taura never edits the .tex; replace numbers with \\res{key} by hand")
    _emit(args, {"literals": [l.to_dict() for l in shown], "summary": summary}, "\n".join(lines))
    return 1 if unique or ambiguous else 0


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
    s.set_defaults(func=cmd_adopt)
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
