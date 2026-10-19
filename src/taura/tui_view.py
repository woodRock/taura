"""The TUI's detail pane: the provenance of the highlighted row as structured, coloured Rich renderables.

Built from the same data as `taura trace` (trace._claim, _artifact, _typed, the run records); only the
presentation differs. The CLI keeps the plain text, which agents and pipes read.
"""

from __future__ import annotations

import re
from datetime import datetime

from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from . import envprobe
from . import trace as tracemod

BADGE = {
    "ok": "black on green", "done": "black on green", "result": "black on green",
    "stale": "black on yellow", "provenance-changed": "black on yellow", "running": "black on cyan",
    "warn": "black on yellow", "unrecorded": "black on yellow", "ambiguous": "black on magenta",
    "matches": "black on cyan", "unmatched": "black on bright_black", "not-a-result": "black on bright_black",
    "dangling": "white on red", "unknown": "white on red", "failed": "white on red", "error": "white on red",
    "unused": "black on bright_black",
}
MUTED = "grey50"
KEY = "bold cyan"


def badge(status: str) -> Text:
    style = BADGE.get(status) or ("black on yellow" if status not in ("", "ok") else "black on green")
    return Text(f" {status} ", style=style)


def short(h: str | None, n: int = 12) -> Text:
    if not h:
        return Text("—", style=MUTED)
    algo, _, digest = h.rpartition(":")
    return Text(f"{algo + ':' if algo else ''}{digest[:n]}…", style=MUTED)


def section(title: str) -> Rule:
    return Rule(Text(title, style="bold"), align="left", style=MUTED)


def kv(rows: list[tuple[str, RenderableType | str | None]]) -> Table:
    t = Table.grid(padding=(0, 2))
    t.add_column(style=MUTED, justify="right", no_wrap=True)
    t.add_column(overflow="fold")
    for k, v in rows:
        if v is None or v == "":
            continue
        t.add_row(k, v if not isinstance(v, str) else Text(v))
    return t


def when(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def duration(start: str | None, end: str | None) -> str | None:
    try:
        s = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    except (TypeError, ValueError):
        return None
    return f"{s / 3600:.1f} h" if s >= 3600 else f"{s / 60:.0f} min" if s >= 90 else f"{s:.0f} s"


def context(text: str, needle: str, title: str) -> Panel:
    """The source line around needle, with needle highlighted."""
    snippet = tracemod.around(text, needle, 220)
    body = Text(snippet, style="default")
    i = snippet.find(needle)
    if i >= 0:
        body.stylize("bold black on yellow", i, i + len(needle))
    return Panel(body, title=Text(title, style=MUTED), title_align="left", border_style=MUTED, padding=(0, 1))


# ---- provenance lines ("pkg 1.2 @ abc1234 (src clean) from ~/x") -------------------------------------

PROV = re.compile(r"^(?P<pkg>\S+) (?P<ver>\S+) @ (?P<commit>\S+) \((?P<state>[^)]*)\)(?: from (?P<path>.*))?$")


def provenance(lines: list[str]) -> RenderableType:
    t = Table(box=None, padding=(0, 1), show_header=True, header_style=MUTED)
    for col in ("package", "version", "commit", "state"):
        t.add_column(col, no_wrap=True)
    other = []
    for line in lines:
        m = PROV.match(line)
        if m:
            state = m.group("state")
            t.add_row(Text(m.group("pkg"), style="bold"), m.group("ver"), Text(m.group("commit")[:7], style="cyan"),
                      Text(state, style="red" if "dirty" in state or "changes" in state else "green"))
        else:
            other.append(Text(line.removeprefix("environment: "), style=MUTED))
    return Group(t, *other) if t.row_count else Group(*other)


def source_view(src: dict) -> list[RenderableType]:
    if not src:
        return []
    out: list[RenderableType] = [section("Source"), kv([("file", Text(src.get("path", "?"), style="bold")),
                                                         ("hash", short(src.get("hash")))])]
    prov = src.get("provenance")
    if isinstance(prov, dict):  # a script's own run, and the run it read: {"script", "ran", "from"}
        out.append(kv([("made by", Text(prov.get("script", "?"), style="bold"))]))
        if prov.get("ran"):
            out += [Text("  ran with", style=MUTED), provenance(prov["ran"])]
        if prov.get("from"):
            out += [Text("  from a run with", style=MUTED), provenance(prov["from"])]
    elif prov:
        out += [Text("  computed with", style=MUTED), provenance(prov)]
    return out


# ---- runs ----------------------------------------------------------------------------------------

DEV_TOOLS = {"pytest", "pytest-cov", "coverage", "ruff", "black", "mypy", "jupyter", "ipykernel", "ipywidgets"}


def env_summary(env: dict, relevant: set[str] | None = None) -> RenderableType | None:
    """Python, GPU and the packages that matter: the ones the repo declares (or, without that,
    the usual numerical stack and editable installs with a commit), plus anything dirty."""
    if not env:
        return None
    pk = env.get("packages", {})
    gpus = env.get("gpus") or []
    gpu = gpus[0].split(",")[1].strip() if gpus and "," in gpus[0] else (gpus[0] if gpus else "none")
    norm = {envprobe._norm(n): n for n in pk}
    if relevant:
        names = [norm[n] for n in sorted(relevant - DEV_TOOLS) if n in norm]
    else:
        names = [n for n in ("torch", "numpy", "scipy", "scikit-learn") if n in pk]
        names += sorted(n for n, v in pk.items() if " @" in v and n not in names)
    names += sorted(n for n, v in pk.items() if "+dirty" in v and n not in names)
    t = Table.grid(padding=(0, 1))
    for n in names:
        v = pk[n]
        version, _, commit = v.partition(" @")
        version, _, how = version.partition(" ")
        t.add_row(Text(n, style="bold"), Text(version),
                  Text(how, style=MUTED),
                  Text("@" + commit, style="bold red" if "+dirty" in commit else "cyan") if commit else Text())
    rest = len(pk) - len(names)
    head = [Text.assemble(f"python {env.get('python', '?')} on {env.get('machine', '?')}"),
            Text.assemble("gpu ", (gpu, "bold") if gpus else (gpu, MUTED), f" ×{len(gpus)}" if len(gpus) > 1 else ""),
            Text.assemble(f"{len(pk)} packages  ", short(env.get("hash"), 10))]
    return Group(*head, t, *([Text(f"+ {rest} more installed", style=MUTED)] if rest > 0 and names else []))


def run_card(rr: dict, relevant: set[str] | None = None) -> RenderableType:
    if rr.get("missing"):
        return Panel(Text(f"{rr['id']}: no record in ledger/runs/", style="red"), border_style="red")
    commit = Text((rr.get("commit") or "?")[:7], style="cyan")
    if rr.get("dirty"):
        commit.append(" dirty", style="bold red")
        if rr.get("base_commit"):
            commit.append(f" (on {rr['base_commit'][:7]})", style=MUTED)
        if rr.get("changed_code"):
            commit.append("  " + ", ".join(rr["changed_code"][:4]) + (" …" if len(rr["changed_code"]) > 4 else ""),
                          style="red")
    if rr.get("changed_outputs"):
        commit.append(f"  · {len(rr['changed_outputs'])} output(s) changed, not code", style=MUTED)
    where = rr.get("host", "?") + (f" · GPU {rr['gpus']}" if rr.get("gpus") not in (None, "") else "")
    if rr.get("spooler_job") is not None:
        where += f" · spooler task {rr['spooler_job']}"
    took = duration(rr.get("started"), rr.get("finished"))
    data = [Text.assemble((name, "bold"), (f" {d['label']}" if d.get("label") else "", ""), "  ", short(d.get("hash"), 10),
                          (f"  {d.get('files', '?')} files" if d.get("files") else "", MUTED),
                          (f"  hashed on {d['hashed_on']}" if d.get("hashed_on") else "", "yellow"))
            for name, d in (rr.get("data") or {}).items()]
    cfg = None
    if rr.get("config_path"):
        cfg = Text.assemble(rr["config_path"], "  ", short(rr.get("config_hash"), 10))
    body = Group(
        kv([("commit", commit), ("where", where),
            ("when", f"{when(rr.get('started'))}" + (f"  ({took})" if took else "")),
            ("command", Text(rr.get("command", ""), style="green")),
            ("config", cfg), ("seed", str(rr["seed"]) if rr.get("seed") is not None else None),
            ("data", Group(*data) if data else None),
            ("note", Text(rr["note"], style="italic yellow") if rr.get("note") else None),
            ("env", env_summary(rr.get("env") or {}, relevant))]),
    )
    title = Text.assemble((rr["id"], "bold"), "  ", badge(rr.get("status", "?")),
                          (f"  exit {rr.get('exit_code', '?')}", MUTED), (f"  · via {rr.get('link')}", MUTED))
    return Panel(body, title=title, title_align="left", border_style="red" if rr.get("dirty") else MUTED,
                 padding=(0, 1))


def runs_view(recs: list[dict] | None, relevant: set[str] | None = None) -> list[RenderableType]:
    out: list[RenderableType] = [section("Runs")]
    if not recs:
        return out + [Text("  none recorded: taura run -- <cmd>, or gpu-spooler with taura installed", style=MUTED)]
    return out + [run_card(r, relevant) for r in recs]


def result_view(r: dict, relevant: set[str] | None = None) -> list[RenderableType]:
    if r.get("missing") and r.get("ledger_error"):
        return [section("Result"), Text("  the code repo's ledger doesn't load:", style="bold red"),
                Text(f"  {r['ledger_error']}", style="red"), Text("  fix it, then press r to reload", style=MUTED)]
    if r.get("missing"):
        return [section("Result"), Text(f"  {r['name']}: not in the code repo's ledger", style="red")]
    changed = Text("changed since publish — run taura diff", style="bold yellow") if r.get("changed_since_publish") else None
    rows = [("result", Text.assemble((r.get("key", r["name"]), KEY))),
            ("recorded", Text(str(r["value"]), style="bold")) if "value" in r else ("", None),
            ("what", r.get("label")), ("file", Text.assemble(r["file"], "  ", short(r.get("hash")))),
            ("by", r.get("produced_by")), ("", changed)]
    return [section("Result"), kv(rows), *source_view(r.get("source") or {}), *runs_view(r.get("run_records"), relevant)]


def published_view(pub: dict, via: dict | None = None) -> list[RenderableType]:
    if not pub.get("published"):
        return [section("Published"), Text("  never", style=MUTED)]
    return [section("Published"), kv([
        ("when", when(pub["published"])), ("from", Text((pub.get("code_commit") or "?")[:7], style="cyan")),
        ("artifact", f"{via['name']} → {via['output']}" if via else None), ("code", pub.get("code_repo"))])]


# ---- the five kinds of row ------------------------------------------------------------------------

def claim_view(it: dict, relevant: set[str] | None = None) -> RenderableType:
    printed = tracemod.readable(it["printed"])
    head = Text.assemble((f"\\res{{{it['key']}}}", KEY), "  ", badge(it["status"]))
    value = Text.assemble(("prints ", MUTED), (printed or "??", "bold magenta" if printed else "bold red"))
    if it["current"] not in (None, it["printed"]):
        value.append(f"   code now gives {tracemod.readable(it['current'])}", style="bold yellow")
    parts: list[RenderableType] = [head, value, Text()]
    if it.get("text"):
        parts.append(context(it["text"], "\\res{" + it["key"] + "}", it.get("location") or ""))
    return Group(*parts, *result_view(it["result"], relevant), *published_view(it["publish"], it.get("artifact")))


def artifact_view(it: dict, relevant: set[str] | None = None) -> RenderableType:
    head = Text.assemble((it["name"], KEY), "  ", (it["artifact_kind"], MUTED), "  ", badge(it["status"]))
    made = it.get("made") or {}
    rows = [("output", Text(it["output"], style="bold")),
            ("used at", ", ".join(it["used_at"]) or Text("nowhere in the paper", style="yellow")),
            ("copied from", made.get("source")), ("made by", made.get("made_by") or made.get("script")),
            ("lock", short(it["lock"].get("hash")) if it["published"] else Text("not published", style="yellow"))]
    parts: list[RenderableType] = [head, Text(), kv(rows)]
    if it["inputs"]:
        parts.append(section("Inputs"))
        t = Table.grid(padding=(0, 2))
        for r in it["inputs"]:
            flag = Text("changed", style="bold yellow") if r.get("changed_since_publish") else Text("✓", style="green")
            t.add_row(Text(r["name"], style=KEY), Text(r.get("label", ""), style=MUTED), flag)
        parts.append(t)
    if it["artifact_kind"] == "figure":
        parts += runs_view(it.get("run_records"), relevant)
    return Group(*parts, *published_view(it["publish"]))


def typed_view(it: dict, relevant: set[str] | None = None) -> RenderableType:
    head = Text.assemble((it["number"], "bold magenta"), "  ", badge(it["verdict"]), ("  typed in the paper", MUTED))
    parts: list[RenderableType] = [head, Text(), context(it.get("sentence") or "", it["number"], it["location"])]
    if it.get("reason"):
        parts += [section("Decision"), kv([("is", Text(f"\\res{{{it['key']}}}", style=KEY) if it.get("key") else it["verdict"]),
                                           ("because", Text(it["reason"], style="italic")),
                                           ("by", it.get("by") or "hand"), ("when", when(it.get("decided")))])]
    elif it["candidates"]:
        parts += [section("Candidates"), *[Text(f"  {c}", style=KEY) for c in it["candidates"][:12]]]
    if it.get("result"):
        parts += result_view(it["result"], relevant)
    return Group(*parts)


def run_view(p, rid: str) -> RenderableType:
    rec = p.runs.records.get(rid, {})
    rr = p.runs.summary(rid, "record")
    parts: list[RenderableType] = [run_card(rr | {"link": "record"}, envprobe.declared(p.code))]
    if rec.get("inputs"):
        parts += [section("Inputs"), *[Text("  " + i) for i in rec["inputs"]]]
    outputs = rec.get("outputs") or {}
    if outputs:
        parts.append(section(f"Outputs ({len(outputs)})"))
        t = Table.grid(padding=(0, 2))
        for path, h in outputs.items():
            t.add_row(Text(path), short(h, 10))
        parts.append(Padding(t, (0, 0, 0, 2)))
    used = p.produced_by_run(rid)
    parts += [section("In the paper"),
              *([Text("  " + u, style=KEY) for u in used] if used else
                [Text("  nothing published comes from this run", style=MUTED)])]
    return Group(*parts)


def finding_view(f) -> RenderableType:
    head = Text.assemble(badge(f.severity), "  ", (f.id, "bold"), "  ", (f"{f.repo}:{f.location}", MUTED))
    parts: list[RenderableType] = [head, Text(), Text(f.message),
                                   Text(), Panel(Text(f.fix), title="How to fix", title_align="left",
                                                 border_style="green", padding=(0, 1))]
    if f.diff:
        parts += [section("Diff against the published version"), Syntax(f.diff, "diff", theme="ansi_dark",
                                                                         background_color="default")]
    return Group(*parts)


def plain(renderable: RenderableType, width: int = 110) -> str:
    """The pane as plain text (tests, copying)."""
    from rich.console import Console
    console = Console(width=width, record=True, file=open("/dev/null", "w"), color_system=None)
    console.print(renderable)
    return console.export_text()
