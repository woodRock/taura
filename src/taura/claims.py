"""Numbers typed into the paper's prose: find them, match them to results, check "N seeds" claims.

Heuristic by design. A typed number *matches* a result when the result, printed at the precision
the number was typed with, gives the same digits: 0.598 matches 0.59847, 4,396 matches 4396,
2.5\\% matches 0.0251 (as a percentage). Lines with `% taura:ignore` are skipped.
"""

from __future__ import annotations

import hashlib
import itertools
import math
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
    # an optional sign that is not part of a range ("5--40") or a word ("pre-2015")
    r"(?P<sign>(?<![\w.\\-])-|\$-\$)?(?<![\w.\\])"
    # 2\times10^{-3} as one number, or a plain number with optional thousands separators
    r"(?:(?P<sci>(?P<mant>\d+(?:\.\d+)?)\s*\\times\s*10\^(?:\{(?P<exp>-?\d+)\}|(?P<exp1>\d)))"
    # a bare power of ten: 10^{-3} (a learning rate), read as 1 x 10^-3
    r"|(?P<pow>10\^(?:\{(?P<pexp>-?\d+)\}|(?P<pexp1>\d)))"
    r"|(?P<num>\d{1,3}(?:(?:,|\{,\})\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?))"
    # a plain number followed by a letter is part of a word or unit ("3D", "2nd"); a power of ten
    # followed by one is a product in math ("10^3N_{eff}")
    r"(?!\.?\d)(?P<pct>\s*\\%)?(?(sci)|(?(pow)|(?![A-Za-z])))"
)
WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())}
REPEATS = re.compile(
    r"\b(?P<n>\d+|" + "|".join(WORDS) + r")\s+(?:random\s+|independent\s+|different\s+)?"
    r"(?P<what>seeds?|runs?|folds?|repeats?|repetitions?|trials?|restarts?)\b", re.I)
MAX_CANDIDATES = 8
SECTION = re.compile(r"\\(?:sub)*section\*?\s*(?:\[[^\]]*\])?\s*\{([^{}]*)\}")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\\(])")
PARAGRAPH_CHARS = 900
RANGE_PERCENT = re.compile(r"\s*(?:--|–|to)\s*\d+(?:\.\d+)?\s*\\%")


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
    masked = DIMENSION.sub(lambda m: " " * len(m.group(0)), "".join(out))
    # Blank control-word names (but keep \\times for 2\\times10^{-3}), so "\\le2" reads as "2".
    return re.sub(r"\\(?!times(?![A-Za-z]))[A-Za-z]+", lambda m: " " * len(m.group(0)), masked)


@dataclass
class Literal:
    file: str
    line: int
    col: int
    text: str  # as typed, e.g. "4{,}396" or "2.5\\%"
    value: str  # digits only: "4396", "2.5"
    percent: bool
    context: str
    negative: bool = False
    exponent: int | None = None  # 2\\times10^{-3}: value "2", exponent -3
    candidates: list[str] = field(default_factory=list)
    more: int = 0  # candidates beyond MAX_CANDIDATES
    suggestion: str | None = None  # \res{key} when exactly one candidate
    renders_as: str | None = None  # what \res{key} prints with the current formats
    format_hint: str | None = None  # a [format] entry that would make it print as typed
    # Context for whoever decides (a person or an agent): where the number sits in the text.
    id: str = ""  # stable across line moves: hash of file, literal and surrounding text
    sentence: str = ""
    paragraph: str = ""
    section: str = ""
    in_caption: bool = False
    options: list[dict] = field(default_factory=list)  # candidate details, filled by match()
    legacy_ids: list[str] = field(default_factory=list)  # ids under older schemes: their decisions apply

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}"

    @property
    def strong(self) -> bool:
        """Precise enough that a match is unlikely to be a coincidence (what `check` warns on)."""
        if self.exponent is not None:
            return False  # p-value bounds: many results round to 2\\times10^{-3}
        if "." not in self.value:
            return self.percent or int(self.value) >= 100  # small counts often match by coincidence
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


# A number or \\res{} with its sign, a power of ten and m\\times10^n included: "$-0.0020", "10^4" and
# "$\\res{key}" (which prints the sign or power) all read as one placeholder.
_VOLATILE = re.compile(r"[-+]?(?:\\" + RES_CMD + r"\{[^{}]*\}"
                       r"|(?:\d+(?:\.\d+)?\s*\\times\s*)?10\^(?:\{-?\d+\}|\d)|\d+(?:(?:[.,]|\{,\})\d+)*)")
# Older schemes, so decisions stored with their ids still apply (newest first).
_VOLATILE_OLD = (
    re.compile(r"[-+]?(?:\\" + RES_CMD + r"\{[^{}]*\}|\d+(?:(?:[.,]|\{,\})\d+)*)"),  # powers of ten split
    re.compile(r"\\" + RES_CMD + r"\{[^{}]*\}|\d+(?:(?:[.,]|\{,\})\d+)*"),  # sign kept
)
ID_WORDS = 6


def literal_id(file: str, text: str, before: str, after: str, volatile: re.Pattern = _VOLATILE) -> str:
    """Stable id: the file, the number, and the words around it with every number and \\res{} collapsed.

    So replacing a *neighbouring* number with \\res{key} (or a line moving) doesn't change it."""
    left = volatile.sub("#", before).split()[-ID_WORDS:]
    right = volatile.sub("#", after).split()[:ID_WORDS]
    key = "\0".join([file, text, " ".join(left), " ".join(right)])
    return hashlib.sha256(key.encode()).hexdigest()[:10]


def _paragraphs(lines: list[tuple[int, str]]) -> list[list[tuple[int, str]]]:
    """Group (line, prose) into paragraphs at blank lines and environment boundaries."""
    paras: list[list[tuple[int, str]]] = [[]]
    for i, code in lines:
        stripped = code.strip()
        boundary = not stripped or stripped.startswith((r"\begin{", r"\end{", r"\section", r"\subsection",
                                                        r"\subsubsection", r"\paragraph"))
        if boundary and paras[-1]:
            paras.append([])
        if stripped:
            paras[-1].append((i, code))
            if boundary:
                paras.append([])
    return [p for p in paras if p]


def literals(paper: Path) -> list[Literal]:
    by_file: dict[str, list[tuple[int, str, str]]] = {}
    for rel, i, code, raw in document_lines(paper):
        by_file.setdefault(rel, []).append((i, code, raw))
    out = []
    for rel, lines in by_file.items():
        raws = {i: raw for i, _, raw in lines}
        section = ""
        for para in _paragraphs([(i, code) for i, code, _ in lines]):
            text, starts = "", {}
            for i, code in para:
                starts[i] = len(text)
                text += code.strip() + " "
            bounds = [0, *(m.end() for m in SENTENCE_END.finditer(text)), len(text)]
            sentences = list(itertools.pairwise(bounds))
            for i, code in para:
                m = SECTION.search(code)
                if m:
                    section = m.group(1).strip()
                if IGNORE in raws[i]:
                    continue
                lead = len(code) - len(code.lstrip())
                for n in NUMBER.finditer(_mask_args(code)):
                    lit_text = code[n.start():n.end()].strip()
                    if n.group("sci"):
                        value, exponent = n.group("mant"), int(n.group("exp") or n.group("exp1"))
                    elif n.group("pow"):
                        value, exponent = "1", int(n.group("pexp") or n.group("pexp1"))
                    else:
                        value, exponent = re.sub(r"\{,\}|,", "", n.group("num")), None
                    ctx = code[max(0, n.start() - 40):n.end() + 40].strip()
                    pos = starts[i] + n.start() - lead
                    sent = next((text[a:b] for a, b in sentences if a <= pos < b), text)
                    lo = max(0, pos - PARAGRAPH_CHARS // 2)
                    # "0.2--5.3\\%": the % sign at the end of a range belongs to both ends
                    pct = bool(n.group("pct")) or bool(RANGE_PERCENT.match(code, n.end()))
                    out.append(Literal(
                        rel, i, n.start() + 1, lit_text, value, pct, ctx,
                        negative=bool(n.group("sign")), exponent=exponent,
                        id=literal_id(rel, lit_text, code[:n.start()], code[n.end():]), sentence=sent.strip(),
                        legacy_ids=[literal_id(rel, lit_text, code[:n.start()], code[n.end():], v)
                                    for v in _VOLATILE_OLD],
                        paragraph=("…" if lo else "") + text[lo:lo + PARAGRAPH_CHARS].strip(),
                        section=section, in_caption=r"\caption" in text[:pos],
                    ))
    _dedupe_ids(out)
    return out


def _dedupe_ids(lits: list[Literal]) -> None:
    """The same number in the same surrounding text twice: number the repeats."""
    seen: dict[str, int] = {}
    for lit in lits:
        k = seen.get(lit.id, 0)
        seen[lit.id] = k + 1
        if k:
            lit.id = f"{lit.id}-{k + 1}"
    for i in range(max((len(lit.legacy_ids) for lit in lits), default=0)):
        seen = {}
        for lit in lits:
            k = seen.get(lit.legacy_ids[i], 0)
            seen[lit.legacy_ids[i]] = k + 1
            if k:
                lit.legacy_ids[i] = f"{lit.legacy_ids[i]}-{k + 1}"


def _matches(lit: Literal, v: float | int, stat: str | None) -> bool:
    if isinstance(v, bool) or (lit.negative and v >= 0):
        return False
    if lit.exponent is not None:  # 2\\times10^{-3}: the mantissa rounded, or rounded up (a bound)
        if v <= 0 or stat == "n":
            return False
        d = len(lit.value.split(".")[1]) if "." in lit.value else 0
        m = v / 10.0 ** lit.exponent
        if f"{m:.{d}f}" == lit.value:
            return True
        # a bound such as p <= 2\\times10^{-3} may be rounded up; a bare 10^{-3} states a value
        return "times" in lit.text and f"{math.ceil(m * 10 ** d - 1e-9) / 10 ** d:.{d}f}" == lit.value
    if "." not in lit.value and not lit.percent:  # integers match recorded whole numbers (counts, or a
        whole = isinstance(v, int) or (isinstance(v, float) and v.is_integer())  # setting such as 20.0),
        return whole and v == int(lit.value)  # not a mean that rounds to it; small ones aren't strong
    if stat == "n":
        return False
    decimals = len(lit.value.split(".")[1]) if "." in lit.value else 0
    scaled = [abs(v) * 100, abs(v)] if lit.percent else [abs(v)]
    return any(_rounds_to(x, decimals, lit.value) for x in scaled)


def _rounds_to(x: float, decimals: int, want: str) -> bool:
    """x printed at `decimals` gives `want`, rounding to nearest, up ("less than 0.003" for 0.0023) or
    down ("r >= 0.99" for 0.996)."""
    up = math.ceil(x * 10 ** decimals - 1e-9) / 10 ** decimals
    down = math.floor(x * 10 ** decimals + 1e-9) / 10 ** decimals
    return want in (f"{x:.{decimals}f}", f"{up:.{decimals}f}", f"{down:.{decimals}f}")


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
    if lit.exponent is not None and not re.search(r"\\times", lit.text):
        opts.append('sci = "power"')
    elif lit.exponent is not None:
        opts += ["sci = true", 'round = "up"']
    return f'[format."{key}"] ' + ", ".join(opts) if opts else None


def match(lits: list[Literal], ledger: Ledger) -> list[Literal]:
    keys = ledger.all_keys()
    for lit in lits:
        cands = sorted(k for k, (_, _, stat, v) in keys.items() if _matches(lit, v, stat))
        lit.candidates = cands  # all of them: an agent must be able to pick any; displays truncate
        lit.more = max(0, len(cands) - MAX_CANDIDATES)
        if len(cands) == 1:
            key = cands[0]
            result, metric, stat, v = keys[key]
            lit.suggestion = rf"\{RES_CMD}{{{key}}}"
            lit.renders_as = ledger.format_for(result, metric, key).render(v, stat)
            lit.format_hint = _format_hint(lit, key, lit.renders_as)
        for key in lit.candidates:
            result, metric, stat, v = keys[key]
            lit.options.append({
                "key": key, "value": v, "renders_as": ledger.format_for(result, metric, key).render(v, stat),
                "result_label": ledger.result(result).get("label", result), "metric": metric, "stat": stat,
            })
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
        keys = list(dict.fromkeys(m.group(1) for text in nearby for m in RES_USE.finditer(text)))
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
                if not isinstance(metric, dict):
                    continue
                # "10 seeds x 5 folds" with n = 50: compare seeds with seeds, folds with folds, runs with n
                # "10 repeats of 5-fold CV": repeats (repetitions) of cross-validation are seeds
                what = m.group("what").lower().rstrip("s")
                stat = {"seed": "seeds", "fold": "folds", "repeat": "seeds", "repetition": "seeds"}.get(what, "n")
                n = metric.get(stat, metric.get("n"))
                if isinstance(n, int) and n != claimed:
                    out.append(SeedClaim(rel, i, claimed, m.group("what"), key, n))
    return out
