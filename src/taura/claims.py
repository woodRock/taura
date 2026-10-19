"""Numbers typed into the paper's prose: find them, match them to results, check "N seeds" claims.

Heuristic by design. A typed number *matches* a result when the result, printed at the precision
the number was typed with, gives the same digits: 0.598 matches 0.59847, 4,396 matches 4396,
2.5\\% matches 0.0251 (as a percentage). Lines with `% taura:ignore` are skipped.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .errors import TauraError
from .generate import RES_CMD
from .ledger import Ledger
from .paper import IGNORE, RES_USE, raw_lines, strip_comment

# Commands whose arguments are never prose numbers: (name, number of brace groups to skip).
SKIP_ARGS = {
    "cite": 1, "citep": 1, "citet": 1, "ref": 1, "eqref": 1, "autoref": 1, "cref": 1, "Cref": 1,
    "label": 1, "url": 1, "href": 1, "includegraphics": 1, "input": 1, "include": 1,
    "bibliography": 1, "bibliographystyle": 1, "begin": 2, "end": 1, "multicolumn": 2,
    "setlength": 2, "addtolength": 2, "vspace": 1, "hspace": 1, "setcounter": 2, "addtocounter": 2,
    "cline": 1, "rule": 2, "phantom": 1, "linespread": 1, "fontsize": 2, RES_CMD: 1,
}
DIMENSION = re.compile(r"\d+(?:\.\d+)?\s*(?:pt|bp|cm|mm|in|em|ex|sp|\\(?:text|column|line)width|\\(?:text|paper)height)")
NUMBER = re.compile(
    r"(?<![\w.\\])(?P<num>\d{1,3}(?:(?:,|\{,\})\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?!\.?\d)(?P<pct>\s*\\%)?(?![A-Za-z])"
)
WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())}
REPEATS = re.compile(
    r"\b(?P<n>\d+|" + "|".join(WORDS) + r")\s+(?:random\s+|independent\s+|different\s+)?"
    r"(?P<what>seeds?|runs?|folds?|repeats?|repetitions?|trials?|restarts?)\b", re.I)
MAX_CANDIDATES = 8


def _mask_args(line: str) -> str:
    """Blank out the arguments of SKIP_ARGS commands (balanced braces) and dimensions."""
    out = list(line)
    for m in re.finditer(r"\\([A-Za-z]+)\*?", line):
        groups = SKIP_ARGS.get(m.group(1))
        if not groups:
            continue
        i = m.end()
        while i < len(line) and line[i] in " [":  # optional arguments
            if line[i] == "[":
                j = line.find("]", i)
                if j < 0:
                    break
                out[i:j + 1] = " " * (j + 1 - i)
                i = j + 1
            else:
                i += 1
        for _ in range(groups):
            if i >= len(line) or line[i] != "{":
                break
            depth, j = 0, i
            while j < len(line):
                depth += {"{": 1, "}": -1}.get(line[j], 0)
                if depth == 0:
                    break
                j += 1
            out[i:j + 1] = " " * (j + 1 - i)
            i = j + 1
    masked = "".join(out)
    return DIMENSION.sub(lambda m: " " * len(m.group(0)), masked)


@dataclass
class Literal:
    file: str
    line: int
    col: int
    text: str  # as typed, e.g. "4{,}396" or "2.5\\%"
    value: str  # digits only: "4396", "2.5"
    percent: bool
    context: str
    candidates: list[str] = field(default_factory=list)
    more: int = 0  # candidates beyond MAX_CANDIDATES
    suggestion: str | None = None  # \res{key} when exactly one candidate
    renders_as: str | None = None  # what \res{key} prints with the current formats
    format_hint: str | None = None  # a [format] entry that would make it print as typed

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}"

    @property
    def strong(self) -> bool:
        """Precise enough that a match is unlikely to be a coincidence (what `check` warns on)."""
        if "." not in self.value:
            return True  # integers only match at >= 100
        return self.percent or len(self.value.split(".")[1]) >= 3

    def to_dict(self) -> dict:
        d = asdict(self)
        d["location"] = self.location
        d["strong"] = self.strong
        return d


def document_lines(paper: Path):
    """(file, line, prose with comments removed, raw line) inside \\begin{document}...\\end{document}.

    Files without \\begin{document} (\\input chapters) are scanned whole."""
    by_file: dict[str, list[tuple[int, str]]] = {}
    for rel, i, raw in raw_lines(paper):
        by_file.setdefault(rel, []).append((i, raw))
    for rel, lines in by_file.items():
        has_doc = any(r"\begin{document}" in strip_comment(r) for _, r in lines)
        inside = not has_doc
        for i, raw in lines:
            code = strip_comment(raw)
            if r"\begin{document}" in code:
                inside = True
                continue
            if r"\end{document}" in code:
                inside = False
            if inside:
                yield rel, i, code, raw


def literals(paper: Path) -> list[Literal]:
    out = []
    for rel, i, code, raw in document_lines(paper):
        if IGNORE in raw:
            continue
        masked = _mask_args(code)
        for m in NUMBER.finditer(masked):
            text = code[m.start():m.end()].strip()
            value = re.sub(r"\{,\}|,", "", m.group("num"))
            ctx = code[max(0, m.start() - 40):m.end() + 40].strip()
            out.append(Literal(rel, i, m.start() + 1, text, value, bool(m.group("pct")), ctx))
    return out


def _matches(lit: Literal, v: float | int, stat: str | None) -> bool:
    if "." not in lit.value:  # integers: only large ones; small ones are too often coincidences
        return isinstance(v, int) and not isinstance(v, bool) and int(lit.value) >= 100 and v == int(lit.value)
    decimals = len(lit.value.split(".")[1])
    if stat == "n" or (decimals < 2 and not lit.percent) or isinstance(v, bool):
        return False
    scaled = [abs(v) * 100, abs(v)] if lit.percent else [abs(v)]
    return any(f"{x:.{decimals}f}" == lit.value for x in scaled)


def _format_hint(lit: Literal, key: str, rendered: str) -> str | None:
    want = lit.text.replace(r"\%", "").strip()
    if rendered == want:
        return None
    opts = []
    if "." in lit.value:
        opts.append(f"decimals = {len(lit.value.split('.')[1])}")
    if lit.percent:
        opts.append("percent = true")
    if "," in lit.text:
        opts.append("thousands = true")
    return f'[format."{key}"] ' + ", ".join(opts) if opts else None


def match(lits: list[Literal], ledger: Ledger) -> list[Literal]:
    keys = ledger.all_keys()
    for lit in lits:
        cands = sorted(k for k, (_, _, stat, v) in keys.items() if _matches(lit, v, stat))
        lit.candidates, lit.more = cands[:MAX_CANDIDATES], max(0, len(cands) - MAX_CANDIDATES)
        if len(cands) == 1:
            key = cands[0]
            result, metric, stat, v = keys[key]
            lit.suggestion = rf"\{RES_CMD}{{{key}}}"
            lit.renders_as = ledger.format_for(result, metric, key).render(v, stat)
            lit.format_hint = _format_hint(lit, key, lit.renders_as)
    return lits


@dataclass
class SeedClaim:
    file: str
    line: int
    claimed: int
    what: str
    key: str
    actual: int

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}"


def seed_claims(paper: Path, ledger: Ledger) -> list[SeedClaim]:
    """"over 5 seeds" next to \\res{r/m/...} whose result has metrics.m.n != 5 (same or next line)."""
    lines = list(document_lines(paper))
    out = []
    for idx, (rel, i, code, raw) in enumerate(lines):
        if IGNORE in raw:
            continue
        nearby = [code] + [lines[j][2] for j in (idx - 1, idx + 1) if 0 <= j < len(lines) and lines[j][0] == rel]
        keys = [m.group(1) for text in nearby for m in RES_USE.finditer(text)]
        for m in REPEATS.finditer(_mask_args(code)):
            raw_n = m.group("n").lower()
            claimed = WORDS[raw_n] if raw_n in WORDS else int(raw_n)
            for key in keys:
                parts = key.split("/")
                if len(parts) < 2:
                    continue
                try:
                    metric = ledger.result(parts[0])["metrics"].get(parts[1])
                except TauraError:
                    continue
                n = metric.get("n") if isinstance(metric, dict) else None
                if isinstance(n, int) and n != claimed:
                    out.append(SeedClaim(rel, i, claimed, m.group("what"), key, n))
    return out
