"""Decisions about numbers typed into the paper: which result each one reports, or that it isn't one.

A decision is made by whoever reads the text (a person, or an agent via `taura resolve` or
`taura adopt --agent CMD`) and recorded in the code repo's ledger/adopt.toml, keyed by the
literal's stable id. taura never edits the .tex: the agent replaces the number with \\res{key},
and `taura check` verifies that the key prints what the paper said.

Verdicts:
  result        the number reports `key`
  not-a-result  a year, postcode, hyperparameter, sample size... leave it typed, stop warning
  unrecorded    a computed result that the ledger doesn't have yet: export it, then decide again
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from . import NAME, tomlio
from .claims import Literal
from .errors import TauraError
from .ledger import LEDGER

DECISIONS_FILE = f"{LEDGER}/adopt.toml"
VERDICTS = ("result", "not-a-result", "unrecorded")


@dataclass
class Decision:
    id: str
    file: str
    literal: str
    context: str
    verdict: str
    key: str = ""
    reason: str = ""
    by: str = ""
    decided: str = ""
    # The result's hash when the rewrite was verified (`taura resolve --stamp`). Once the result
    # changes (a rerun), the paper rightly prints a new value: adopt-mismatch no longer applies.
    result_hash: str = ""


def load(code: Path) -> dict[str, Decision]:
    path = code / DECISIONS_FILE
    if not path.is_file():
        return {}
    out = {}
    for lid, d in tomlio.load(path).get("decisions", {}).items():
        try:
            out[lid] = Decision(id=lid, **d)
        except TypeError as e:
            raise TauraError(f"{path}: decision {lid}: {e}") from None
        if out[lid].verdict not in VERDICTS:
            raise TauraError(f"{path}: decision {lid}: verdict must be one of {', '.join(VERDICTS)}")
    return out


def save(code: Path, decisions: dict[str, Decision]) -> None:
    body = {lid: {k: v for k, v in asdict(d).items() if k != "id"} for lid, d in sorted(decisions.items())}
    header = (f"# Decisions about numbers typed into the paper ({NAME} adopt / resolve).\n"
              "# verdict: result (key) | not-a-result | unrecorded. Keyed by literal id.\n\n")
    (code / DECISIONS_FILE).write_text(header + tomlio.dumps({"decisions": body}), encoding="utf-8")


def make(lit: Literal, verdict: str, key: str, reason: str, by: str) -> Decision:
    if verdict not in VERDICTS:
        raise TauraError(f"verdict must be one of {', '.join(VERDICTS)}")
    if verdict == "result":
        if key not in lit.candidates:
            options = ", ".join(lit.candidates) or "none (try --unrecorded or --none)"
            raise TauraError(f"{lit.text} at {lit.location} doesn't match {key!r}; matching keys: {options}")
    elif key:
        raise TauraError(f"a {verdict} decision takes no key")
    if not reason.strip():
        raise TauraError("give a --reason: one sentence on what in the text decided it")
    return Decision(lit.id, lit.file, lit.text, lit.context, verdict, key, reason.strip(), by,
                    datetime.now().astimezone().isoformat(timespec="seconds"))


def apply_to(lits: list[Literal], decisions: dict[str, Decision]) -> dict[str, Decision]:
    """The decisions that still refer to a typed number in the text (by id)."""
    ids = {lit.id for lit in lits}
    return {lid: d for lid, d in decisions.items() if lid in ids}


def needs_agent(lit: Literal, include_unique: bool) -> bool:
    """Which undecided numbers go to an agent: ties, weak matches, and unmatched numbers."""
    if len(lit.candidates) > 1 or not lit.candidates:
        return True
    return include_unique or not lit.strong


# ---- asking an agent ------------------------------------------------------------------

PROMPT = """You are helping migrate a LaTeX paper to generated results. Each item below is a number that
was typed into the paper by hand, with the text around it and the recorded results whose value
matches it (at the precision it was typed).

For each item decide, from the text, what the number is:
- "result": it reports one of the candidate results. Give its "key". Read the sentence: it names
  the method, batch pair, group or quantity, which tells you which candidate it is. Candidates whose
  key starts with "settings-" are settings read from the code's config files and constants
  (hyperparameters, folds, seeds, sizes): a hyperparameter or setting the text states is that key.
  But a value chosen by a search or by cross-validation is a result, not a setting.
- "not-a-result": it is neither a computed result nor a recorded setting (a year, postcode, page
  or section number, a hyperparameter with no settings- candidate, a count of samples or batches
  that describes the data, a value quoted from another paper, a range bound in a description). Also
  use this when a candidate matches only by coincidence (e.g. a correlation coefficient that happens
  to equal a TIC r to two decimals).
- "unrecorded": it IS a computed result of this paper's experiments (an error, score, count of
  anchors, coverage, timing...) but none of the candidates is it, or there are no candidates.

Reply with JSON only, no prose, in exactly this shape:
{"decisions": [{"id": "<item id>", "verdict": "result" | "not-a-result" | "unrecorded",
                "key": "<candidate key, only for result>", "reason": "<one short sentence citing the text>"}]}

Items:
"""


def packet(lit: Literal) -> dict:
    return {
        "id": lit.id, "number": lit.text, "location": lit.location, "section": lit.section,
        "in_caption": lit.in_caption, "sentence": lit.sentence, "paragraph": lit.paragraph,
        "candidates": [{k: o[k] for k in ("key", "renders_as", "result_label", "metric", "stat")} for o in lit.options],
    }


def _parse_reply(text: str) -> list[dict]:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise TauraError("agent reply contained no JSON object")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise TauraError(f"agent reply is not valid JSON: {e}") from None
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        raise TauraError('agent reply needs {"decisions": [...]}')
    return data["decisions"]


def ask_agent(cmd: str, lits: list[Literal], batch: int = 20, timeout: int = 600,
              log=lambda msg: None) -> tuple[list[Decision], list[str]]:
    """Send literals to `cmd` (prompt on stdin, JSON on stdout) in batches.

    Returns (valid decisions, problems). Replies are checked like `taura resolve`: a "result"
    must name one of that number's candidates, so an agent can't invent a key."""
    argv = shlex.split(cmd)
    if not argv:
        raise TauraError("--agent needs a command, e.g. --agent \"claude -p\"")
    by = Path(argv[0]).name
    out, problems = [], []
    for start in range(0, len(lits), batch):
        chunk = lits[start:start + batch]
        log(f"asking {by} about {len(chunk)} number(s) ({start + 1}-{start + len(chunk)} of {len(lits)})")
        prompt = PROMPT + json.dumps([packet(lit) for lit in chunk], indent=1, ensure_ascii=False)
        try:
            proc = subprocess.run(argv, input=prompt, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            raise TauraError(f"agent command not found: {argv[0]}") from None
        except subprocess.TimeoutExpired:
            problems.append(f"batch {start // batch + 1}: {by} timed out after {timeout}s")
            continue
        if proc.returncode != 0:
            problems.append(f"batch {start // batch + 1}: {by} exited {proc.returncode}: {proc.stderr.strip()[-300:]}")
            continue
        try:
            replies = _parse_reply(proc.stdout)
        except TauraError as e:
            problems.append(f"batch {start // batch + 1}: {e}")
            continue
        by_id = {lit.id: lit for lit in chunk}
        for r in replies:
            lit = by_id.pop(str(r.get("id")), None) if isinstance(r, dict) else None
            if lit is None:
                problems.append(f"agent answered about an unknown or repeated id: {r!r}"[:200])
                continue
            try:
                out.append(make(lit, str(r.get("verdict", "")), str(r.get("key") or ""),
                                str(r.get("reason", "")), by))
            except TauraError as e:
                problems.append(f"{lit.location} {lit.text} ({lit.id}): rejected: {e}")
        problems += [f"{lit.location} {lit.text} ({lit.id}): no answer" for lit in by_id.values()]
    return out, problems
