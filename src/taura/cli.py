"""Command-line interface. Exit codes: 0 clean, 1 findings/refusal, 2 usage or config error."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import socket
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from . import NAME, __version__, claims, decisions, envprobe, gitutil, runs
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
    if args.redo:  # ask again about these verdicts (e.g. after adding settings)
        decided = {k: d for k, d in decided.items() if d.verdict not in args.redo}
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


def _code(args) -> Path:
    """The code repo: the paired one, or (outside the roots) the git repo here if it has a ledger."""
    try:
        return _pair(args).code
    except TauraError as e:
        here = Path(args.C).expanduser() if args.C else Path.cwd()
        r = gitutil.git(here, "rev-parse", "--show-toplevel", check=False)
        top = Path(r.stdout.strip()) if r.returncode == 0 else None
        if top and (top / "ledger").is_dir():
            return top
        raise e


def cmd_run(args) -> int:
    argv = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    if not argv:
        raise TauraError("nothing to run: taura run -- <command> [args...]")
    code = _code(args)
    cwd = Path(args.C).expanduser().resolve() if args.C else Path.cwd()
    run_id = args.id or runs.new_id()
    rec = runs.start_record(code, run_id, argv, cwd=cwd, runner=NAME) | runs.git_state(code)
    python = envprobe.interpreter(argv)
    if python:
        rec["env"] = envprobe.probe(python, cwd)
    where = runs.save(code, rec)
    if rec["dirty"]:
        print(f"{NAME}: note: the code repo has uncommitted changes; run {run_id} is recorded as dirty",
              file=sys.stderr)
    before = runs.scan(code)
    try:
        rc = subprocess.run(argv, cwd=cwd, env={**os.environ, f"{NAME.upper()}_RUN_ID": run_id}).returncode
    except FileNotFoundError:
        print(f"{NAME}: command not found: {argv[0]}", file=sys.stderr)
        rc = 127
    except KeyboardInterrupt:
        rc = 130
    outs = runs.outputs(code, before, runs.scan(code), args.outputs or [])
    rec |= {"status": "done" if rc == 0 else "failed", "finished": runs.now(), "exit_code": rc,
            "outputs": outs or None}
    runs.save(code, rec)
    summary = f"{NAME}: run {run_id} ({rec['status']}, exit {rc}, {len(outs)} output(s)) → {where.relative_to(code)}"
    if args.json:
        print(json.dumps(rec, indent=2, sort_keys=True))
    else:
        print(summary, file=sys.stderr)
    return rc


def cmd_record(args) -> int:
    code = _code(args)
    rec = runs.load(code, args.id)
    new = rec is None
    if new:
        argv = shlex.split(args.command) if args.command else []
        base = (code / args.cwd) if args.cwd else code
        rec = runs.start_record(code, args.id, argv, command=args.command, cwd=base,
                                runner=args.runner) if args.event in (None, "start") or argv else {"id": args.id}
    if new or args.commit:
        rec |= runs.git_state(code, args.commit, args.dirty_count if args.commit else None)
    for field, value in (("host", args.host), ("gpus", args.gpus), ("spooler_job", args.spooler_job),
                         ("runner", args.runner), ("cwd", args.cwd), ("note", args.note)):
        if value is not None:
            rec[field] = value
    here = socket.gethostname()
    if rec.get("host") and rec["host"] != here:  # ran elsewhere: say where the data was hashed
        for d in (rec.get("data") or {}).values():
            d.setdefault("hashed_on", here)
    if args.started:
        rec["started"] = runs.iso(args.started)
    if args.event == "finish" or args.exit_code is not None:
        rec["finished"] = runs.iso(args.finished) or runs.now()
        if args.exit_code is not None:
            rec["exit_code"] = args.exit_code
        rec["status"] = "done" if args.exit_code == 0 else "failed"
    if args.outputs:
        rec["outputs"] = dict(rec.get("outputs") or {}) | runs.hash_outputs(code, args.outputs)
    if args.env_json:
        try:
            rec["env"] = envprobe.finish(json.loads(Path(args.env_json).read_text()))
        except (OSError, json.JSONDecodeError) as e:
            raise TauraError(f"--env-json {args.env_json}: {e}") from None
    where = runs.save(code, rec)
    _emit(args, rec, f"{'recorded' if new else 'updated'} run {args.id} → {where.relative_to(code)}")
    return 0


def _diff_lines(d: dict, a: str, b: str) -> list[str]:
    lines = [f"{a}  vs  {b}"]
    for k, v in d.items():
        if k in ("data", "env", "outputs"):
            continue
        lines.append(f"  {k:<12} {v[0]}  →  {v[1]}")
    for name, (x, y) in (d.get("data") or {}).items():
        lines.append(f"  data {name:<7} {x}  →  {y}")
    env = d.get("env") or {}
    for k in ("python", "platform", "gpus", "executable", "recorded"):
        if k in env:
            lines.append(f"  env {k:<8} {env[k][0]}  →  {env[k][1]}")
    for name, (x, y) in (env.get("packages") or {}).items():
        lines.append(f"  package {name:<20} {x or '(none)'}  →  {y or '(none)'}")
    o = d["outputs"]
    lines.append(f"  outputs      {len(o['identical'])} identical, {len(o['different'])} different, "
                 f"{len(o['only_in_first'])} only in {a}, {len(o['only_in_second'])} only in {b}")
    lines += [f"    different: {n}" for n in o["different"][:20]]
    if len(d) == 1 and not o["different"] and not o["only_in_first"] and not o["only_in_second"]:
        lines.append("  no differences")
    return lines


def cmd_runs(args) -> int:
    code = _code(args)
    recs = runs.load_all(code)
    if args.diff:
        a, b = args.diff
        missing = [r for r in (a, b) if r not in recs]
        if missing:
            raise TauraError(f"no run record for {', '.join(missing)}; see `taura runs`")
        d = runs.diff(recs[a], recs[b])
        _emit(args, {"a": a, "b": b, "diff": d}, "\n".join(_diff_lines(d, a, b)))
        return 0
    lines = [f"{r.get('id'):<34} {r.get('status', '?'):<8} {(r.get('commit') or '?')[:7]}"
             f"{'+dirty' if r.get('dirty') else '      '} {(r.get('host') or '?'):<14} {r.get('command', '')[:70]}"
             for r in recs.values()]
    _emit(args, {"runs": list(recs.values())}, "\n".join(lines) or "no run records yet")
    return 0


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
    s.add_argument("--redo", nargs="+", choices=list(decisions.VERDICTS), metavar="VERDICT",
                   help="treat decisions with these verdicts as undecided (e.g. --redo not-a-result after "
                        "adding [settings])")
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
    s = sub.add_parser("run", parents=[common],
                       help="run a command and record it: commit, config, seed, data, outputs (exit code passes through)")
    s.add_argument("--id", help="run id (default: date-time-random)")
    s.add_argument("--outputs", nargs="*", metavar="PATH", help="outputs to record even if they didn't change")
    s.add_argument("cmd", nargs=argparse.REMAINDER, help="-- the command and its arguments")
    s.set_defaults(func=cmd_run)
    s = sub.add_parser("record", parents=[common],
                       help="write or complete a run record for a command run elsewhere (e.g. by gpu-spooler)")
    s.add_argument("id", help="run id")
    s.add_argument("--event", choices=["start", "finish", "fetch"], help="what happened (default: just update)")
    s.add_argument("--command", help="the command line that ran")
    s.add_argument("--cwd", help="its working directory, relative to the code repo")
    s.add_argument("--commit", help="the commit it ran at (default: this repo's HEAD)")
    s.add_argument("--dirty-count", type=int, default=0, help="uncommitted changes where it ran")
    s.add_argument("--host", help="where it ran")
    s.add_argument("--gpus", help="GPU indices")
    s.add_argument("--spooler-job", type=int, help="gpu-spooler task id")
    s.add_argument("--runner", help="what ran it, e.g. gpu-spooler")
    s.add_argument("--started", help="start time (ISO or epoch seconds)")
    s.add_argument("--finished", help="finish time (ISO or epoch seconds)")
    s.add_argument("--exit-code", type=int, help="its exit code (marks it finished)")
    s.add_argument("--outputs", nargs="*", metavar="PATH", help="output files or directories, hashed now")
    s.add_argument("--env-json", metavar="FILE", help="the run's environment, as printed by taura's probe")
    s.add_argument("--note", help="a remark kept with the record (e.g. how it was reconstructed)")
    s.set_defaults(func=cmd_record)
    s = sub.add_parser("runs", parents=[common], help="list run records, or --diff two of them")
    s.add_argument("--diff", nargs=2, metavar=("RUN_A", "RUN_B"),
                   help="what differs: commit, command, config, seed, host, data, environment, outputs")
    s.set_defaults(func=cmd_runs)
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
