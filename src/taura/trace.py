"""`taura trace TARGET`: walk a claim in the paper back to the results, files and commits behind it.

TARGET is a \\res key (`mba/f1/mean`, or `res:mba/f1/mean`), a place in the paper (`main.tex:142`:
everything on that line), or an artifact name (`table_main`).
"""

from __future__ import annotations

import re

from .errors import TauraError
from .model import ArtifactView, Claim, Project, Typed

LOCATION = re.compile(r"^(?P<file>.+\.tex):(?P<line>\d+)$")


def _claim(p: Project, c: Claim) -> dict:
    result, metric, stat = (p.keys[c.key][:3] if c.key in p.keys else (c.result, None, None))
    via = next((a for a in p.artifacts if a.kind == "macros" and a.published), None)
    return {
        "kind": "claim", "location": c.location, "text": c.text, "key": c.key, "status": c.status,
        "printed": c.printed, "current": c.current,
        "result": p.result_info(result, metric, stat),
        "artifact": {"name": via.name, "output": via.output} if via else None,
        "publish": p.publish_info,
    }


def _artifact(p: Project, a: ArtifactView) -> dict:
    entry = p.lock["artifacts"].get(a.name, {})
    art = p.ledger.artifacts.get(a.name) if p.ledger else None
    inputs = art.results if art else entry.get("inputs", [])
    out = {
        "kind": "artifact", "name": a.name, "artifact_kind": a.kind, "output": a.output, "status": a.status,
        "used_at": a.used_at, "published": a.published,
        "lock": {k: entry[k] for k in ("hash", "script", "script_hash", "config_hash", "source") if k in entry},
        "inputs": [p.result_info(r) for r in inputs],
        "publish": p.publish_info,
    }
    if art is not None and (art.source or art.script):
        out["made"] = {"script": art.script, "source": art.source, "made_by": art.made_by}
    if a.kind == "figure":
        out["run_records"] = p.runs_for(None, entry.get("hash"))
    return out


def _typed(p: Project, t: Typed) -> dict:
    d = t.decision
    out = {"kind": "typed", "location": t.literal.location, "id": t.literal.id, "number": t.literal.text,
           "verdict": t.verdict, "sentence": t.literal.sentence, "candidates": t.literal.candidates}
    if d is not None:
        out |= {"key": d.key, "reason": d.reason, "by": d.by, "decided": d.decided}
        if d.key:
            r, m, s = p.keys[d.key][:3] if d.key in p.keys else (d.key.split("/")[0], None, None)
            out["result"] = p.result_info(r, m, s)
    return out


def trace(p: Project, target: str) -> dict:
    target = target.strip()
    items: list[dict] = []
    loc = LOCATION.match(target)
    if loc:
        rel, line = loc.group("file"), int(loc.group("line"))
        items += [_claim(p, c) for c in p.claims if c.file == rel and c.line == line]
        items += [_artifact(p, a) for a in p.artifacts if f"{rel}:{line}" in a.used_at]
        items += [_typed(p, t) for t in p.typed if t.literal.file == rel and t.literal.line == line]
        kind = "location"
        if not items and not p.line_text(rel, line):
            raise TauraError(f"{target}: no such line in the paper")
    elif any(a.name == target for a in p.artifacts):
        items = [_artifact(p, next(a for a in p.artifacts if a.name == target))]
        kind = "artifact"
    else:
        key = target.removeprefix("res:")
        if key not in p.keys and key not in p.published:
            raise TauraError(
                f"{target!r} is not a \\res key, a paper location (file.tex:LINE) or an artifact; "
                f"artifacts: {', '.join(a.name for a in p.artifacts) or 'none'}")
        uses = [c for c in p.claims if c.key == key]
        if uses:
            items = [_claim(p, c) for c in uses]
        else:  # a key the paper doesn't use (yet)
            printed = p.published.get(key)
            c = Claim("", 0, key, printed, p.render(key), "unused", "")
            items = [_claim(p, c) | {"location": None}]
        items += [_typed(p, t) for t in p.typed if t.decision and t.decision.key == key]
        kind = "key"
    return {"target": target, "kind": kind, "paper": p.name, "items": items}


# ---- text ---------------------------------------------------------------------------


def readable(value: str | None) -> str | None:
    """A macro value as a reader sees it: 4{,}396 -> 4,396, 2\\times10^{-3} -> 2×10^-3, $-$0.4 -> -0.4."""
    if value is None:
        return None
    value = value.replace("{,}", ",").replace("$-$", "-")
    return re.sub(r"\\times\s*10\^\{?(-?\d+)\}?", r"×10^\1", value)


def around(text: str, needle: str, width: int = 150) -> str:
    """The part of a (long) source line around needle."""
    i = text.find(needle)
    if i < 0 or len(text) <= width:
        return text[:width]
    lo = max(0, i - (width - len(needle)) // 2)
    hi = min(len(text), lo + width)
    return ("…" if lo else "") + text[lo:hi] + ("…" if hi < len(text) else "")


def _short(h: str | None) -> str:
    return (h or "")[:19] + ("…" if h and len(h) > 19 else "")


def _result_lines(r: dict, indent: str) -> list[str]:
    if r.get("missing"):
        return [f"{indent}result   {r['name']}: not in the code repo's ledger"]
    value = f"{r.get('key', r['name'])} = {r['value']}" if "value" in r else r["name"]
    out = [f"{indent}result   {value}  ({r.get('label', '')})",
           f"{indent}         {r['file']}  {_short(r.get('hash'))}"
           + ("  CHANGED since publish" if r.get("changed_since_publish") else "")]
    src = r.get("source") or {}
    if src:
        out.append(f"{indent}source   {src.get('path', '?')}  {_short(src.get('hash'))}")
        prov = src.get("provenance")
        if isinstance(prov, dict):  # script 04 style: {"script", "ran", "from"}
            out += [f"{indent}         made by {prov.get('script', '?')}"]
            out += [f"{indent}           ran:  {line}" for line in prov.get("ran", [])]
            out += [f"{indent}           from: {line}" for line in prov.get("from", [])]
        elif prov:
            out += [f"{indent}         {line}" for line in prov]
    if r.get("produced_by"):
        out.append(f"{indent}by       {r['produced_by']}")
    out += _run_lines(r.get("run_records"), indent)
    return out


def _run_lines(recs: list[dict] | None, indent: str) -> list[str]:
    if not recs:
        return [f"{indent}runs     none recorded (taura run -- <cmd>, or gpu-spooler with taura installed)"]
    out = []
    for i, rr in enumerate(recs):
        head = f"{indent}{'runs' if i == 0 else '':<9}{rr['id']}"
        if rr.get("missing"):
            out.append(head + "  (no record in ledger/runs/)")
            continue
        commit = (rr.get("commit") or "?")[:7]
        if rr.get("dirty"):
            commit += f" dirty (on {rr['base_commit'][:7]})" if rr.get("base_commit") else " dirty"
        where = rr.get("host", "?") + (f" gpus {rr['gpus']}" if rr.get("gpus") else "")
        job = f", spooler task {rr['spooler_job']}" if rr.get("spooler_job") is not None else ""
        out.append(f"{head}  [{rr.get('status', '?')}, exit {rr.get('exit_code', '?')}]  via {rr['link']}")
        out.append(f"{indent}           commit {commit}  on {where}{job}  {rr.get('started', '')}")
        out.append(f"{indent}           $ {rr.get('command', '')}")
        cfg = f"config {rr['config_path']} {_short(rr.get('config_hash'))}" if rr.get("config_path") else ""
        seed = f"seed {rr['seed']}" if rr.get("seed") is not None else ""
        if cfg or seed:
            out.append(f"{indent}           " + "  ".join(x for x in (cfg, seed) if x))
        for name, d in (rr.get("data") or {}).items():
            label = f" {d['label']}" if d.get("label") else ""
            out.append(f"{indent}           data {name}{label}  {_short(d.get('hash'))}  ({d.get('files', '?')} files)")
    return out


def _publish_line(pub: dict, indent: str) -> str:
    if not pub.get("published"):
        return f"{indent}publish  never"
    return f"{indent}publish  {pub['published']} from {(pub.get('code_commit') or '?')[:7]} ({pub.get('code_repo')})"


def render(t: dict) -> str:
    out = [f"{t['paper']}: {t['target']}"]
    if not t["items"]:
        out.append("  nothing traceable on this line (no \\res, generated artifact or typed number)")
    for it in t["items"]:
        out.append("")
        if it["kind"] == "claim":
            where = it["location"] or "(not used in the paper)"
            now = "" if it["current"] in (None, it["printed"]) else f", code now gives {it['current']}"
            out.append(f"  {where}  \\res{{{it['key']}}} prints {readable(it['printed']) or '??' + it['key'] + '??'}"
                       f"  [{it['status']}{now}]")
            if it["text"]:
                needle = "\\res{" + it["key"] + "}"
                out.append("    " + around(it["text"], needle))
            out += _result_lines(it["result"], "    ")
            if it["artifact"]:
                out.append(f"    via      artifact {it['artifact']['name']} → {it['artifact']['output']}")
            out.append(_publish_line(it["publish"], "    "))
        elif it["kind"] == "artifact":
            out.append(f"  artifact {it['name']} ({it['artifact_kind']}) → {it['output']}  [{it['status']}]")
            out.append("    used at  " + (", ".join(it["used_at"]) or "nowhere in the paper"))
            made = it.get("made") or {}
            if made.get("source"):
                out.append(f"    copied   {made['source']}" + (f" (made by {made['made_by']})" if made.get("made_by") else ""))
            if made.get("script"):
                out.append(f"    script   {made['script']}  {_short(it['lock'].get('script_hash'))}")
            out.append(f"    lock     {_short(it['lock'].get('hash')) or 'not published'}")
            if it["artifact_kind"] == "figure":
                out += _run_lines(it.get("run_records"), "    ")
            for r in it["inputs"]:
                out += _result_lines(r, "    ")
            out.append(_publish_line(it["publish"], "    "))
        else:
            what = f"\\res{{{it['key']}}}" if it.get("key") else it["verdict"]
            out.append(f"  {it['location']}  typed {it['number']}  → {what}")
            if it.get("reason"):
                out.append(f"    decided  {it['reason']}  ({it.get('by') or 'by hand'})")
            elif it["candidates"]:
                out.append(f"    candidates {', '.join(it['candidates'][:8])}")
            if it.get("result"):
                out += _result_lines(it["result"], "    ")
    return "\n".join(out)
