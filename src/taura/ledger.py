"""The code repo's ledger: results, the artifact map and number formatting."""

from __future__ import annotations

import fnmatch
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import tomlio
from .errors import TauraError
from .hashing import sha256_file, sha256_json

LEDGER = "ledger"
RESULTS_DIR = f"{LEDGER}/results"
RUNS_DIR = f"{LEDGER}/runs"
ARTIFACTS_FILE = f"{LEDGER}/artifacts.toml"

KINDS = ("macros", "table", "figure")
# Characters allowed in result names, metric names and stats. They end up inside
# \csname...\endcsname and \res{...}, so nothing with TeX meaning ({}%#\$&^~).
KEY_PART = re.compile(r"^[A-Za-z0-9_.\-]+$")


FORMAT_OPTIONS = ("decimals", "percent", "thousands", "sign", "sci", "round")


@dataclass
class Format:
    decimals: int = 3
    percent: bool = False  # 0.912 -> 91.2 (the % sign stays in the .tex)
    thousands: bool = False  # 4396 -> 4{,}396
    sign: bool = False  # explicit + on positive numbers
    sci: bool = False  # 0.00195 -> 2\\times10^{-3} (math mode); decimals apply to the mantissa
    round: str = "nearest"  # or "up": for bounds such as p <= ..., so rounding never makes them false

    def _round(self, x: float, decimals: int) -> float:
        if self.round == "up":
            return math.ceil(x * 10 ** decimals - 1e-9) / 10 ** decimals
        if self.round != "nearest":
            raise TauraError(f'format round must be "nearest" or "up", got {self.round!r}')
        return float(f"{x:.{decimals}f}")

    def render(self, value, stat: str | None = None) -> str:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TauraError(f"cannot format non-number {value!r}")
        if self.sci and value != 0 and stat != "n":
            e = math.floor(math.log10(abs(value)))
            m = self._round(abs(value) / 10.0 ** e, self.decimals)
            if m >= 10:
                m, e = m / 10, e + 1
            return ("-" if value < 0 else "") + f"{m:.{self.decimals}f}\\times10^{{{e}}}"
        if stat == "n" or (isinstance(value, int) and not self.percent):
            s = f"{int(value):,}" if self.thousands else str(int(value))
        else:
            x = self._round(value * 100 if self.percent else value, self.decimals)
            s = f"{x:,.{self.decimals}f}" if self.thousands else f"{x:.{self.decimals}f}"
            if float(s.replace(",", "")) == 0:  # no "-0.000"
                s = s.lstrip("-")
        if self.sign and not s.startswith("-") and float(s.replace(",", "")) != 0:
            s = "+" + s
        return s.replace(",", "{,}")


@dataclass
class Artifact:
    name: str
    kind: str
    output: str
    results: list[str]
    script: str | None = None
    bold: str | None = None
    options: dict = field(default_factory=dict)  # everything else from the TOML table
    source: str | None = None  # figure: a file the code repo's own pipeline writes, copied as is
    made_by: str | None = None  # figure: the script that writes `source` (for staleness)

    @property
    def is_binary(self) -> bool:
        return not self.output.endswith(".tex")


class Ledger:
    def __init__(self, code: Path):
        self.code = code
        self._results: dict[str, dict] | None = None
        path = code / ARTIFACTS_FILE
        if not path.is_file():
            raise TauraError(f"no artifact map at {path} (run `taura init`)")
        self.config = tomlio.load(path)
        self.artifacts = self._parse_artifacts(path)
        self.formats = self.config.get("format", {})

    # ---- artifacts ---------------------------------------------------------

    def _parse_artifacts(self, path: Path) -> dict[str, Artifact]:
        out: dict[str, Artifact] = {}
        outputs: dict[str, str] = {}
        for name, spec in sorted(self.config.get("artifacts", {}).items()):
            where = f"{path}: [artifacts.{name}]"
            if not isinstance(spec, dict):
                raise TauraError(f"{where} must be a table")
            spec = dict(spec)
            kind = spec.pop("kind", None)
            if kind not in KINDS:
                raise TauraError(f"{where}: kind must be one of {', '.join(KINDS)}, got {kind!r}")
            output = spec.pop("output", None)
            if not output or not output.startswith("generated/") or ".." in Path(output).parts:
                raise TauraError(f"{where}: output must be a path under generated/, got {output!r}")
            if output in outputs:
                raise TauraError(f"{where}: output {output} also produced by {outputs[output]}")
            outputs[output] = name
            results = spec.pop("results", [])
            if not isinstance(results, list) or not all(isinstance(r, str) for r in results):
                raise TauraError(f"{where}: results must be a list of result names")
            if any(c in r for r in results for c in "*?["):  # globs, e.g. ["*"] or ["ticr-*"]
                names = sorted(load_results(self.code))
                results = sorted({n for r in results for n in (fnmatch.filter(names, r) or [r])})
            script = spec.pop("script", None)
            bold = spec.pop("bold", None)
            source = spec.pop("source", None)
            made_by = spec.pop("made_by", None)
            if kind == "figure":
                if bool(script) == bool(source):
                    raise TauraError(f"{where}: a figure needs either `script` (taura runs it) or `source` "
                                     "(a PDF the code repo already writes)")
                if not output.endswith(".pdf") or (source and not source.endswith(".pdf")):
                    raise TauraError(f"{where}: figures are PDF only")
                if made_by and not source:
                    raise TauraError(f"{where}: `made_by` goes with `source`")
                if made_by and not (self.code / made_by).is_file():
                    raise TauraError(f"{where}: made_by {made_by} not found")
            elif source or made_by:
                raise TauraError(f"{where}: `source` and `made_by` are for figures")
            elif not output.endswith(".tex"):
                raise TauraError(f"{where}: {kind} output must be a .tex file")
            if kind == "macros" and script:
                raise TauraError(f"{where}: macros are generated from results; drop `script`")
            if bold not in (None, "max", "min"):
                raise TauraError(f"{where}: bold must be \"max\" or \"min\"")
            if script and not (self.code / script).is_file():
                raise TauraError(f"{where}: script {script} not found")
            out[name] = Artifact(name, kind, output, results, script, bold, spec, source, made_by)
        return out

    def select(self, names: list[str] | None) -> list[Artifact]:
        if not names:
            return list(self.artifacts.values())
        unknown = [n for n in names if n not in self.artifacts]
        if unknown:
            raise TauraError(
                f"unknown artifact(s): {', '.join(unknown)}; known: {', '.join(self.artifacts) or 'none'}"
            )
        return [self.artifacts[n] for n in names]

    def config_hash(self, art: Artifact) -> str:
        """Hash of everything in the config that shapes this artifact's output."""
        cfg = {
            "kind": art.kind,
            "output": art.output,
            "results": art.results,
            "bold": art.bold,
            "options": art.options,
            "format": self.formats,
        }
        if art.source:  # only when set, so adding the field didn't invalidate existing locks
            cfg |= {"source": art.source, "made_by": art.made_by}
        return sha256_json(cfg)

    def script_hash(self, art: Artifact) -> str | None:
        script = art.script or art.made_by
        return sha256_file(self.code / script) if script else None

    # ---- results -----------------------------------------------------------

    @property
    def results(self) -> dict[str, dict]:
        if self._results is None:
            self._results = load_results(self.code)
        return self._results

    def result(self, name: str) -> dict:
        if name not in self.results:
            raise TauraError(f"result {name!r} not found in {self.code / RESULTS_DIR}")
        return self.results[name]

    def result_hash(self, name: str) -> str:
        return sha256_json(self.result(name))

    # ---- formatting --------------------------------------------------------

    def format_for(self, result: str | None, metric: str | None, key: str | None = None) -> Format:
        """[format.default], then [format.<metric>], then [format."<result>/<metric>"], then any
        glob pattern matching the full key (e.g. "pairs/*/anchors"; later patterns win), then the
        exact full key (e.g. "cycle/joint/max")."""
        merged = dict(self.formats.get("default", {}))
        if metric:
            merged.update(self.formats.get(metric, {}))
            if result:
                merged.update(self.formats.get(f"{result}/{metric}", {}))
        if key:
            for pattern, opts in self.formats.items():
                if any(c in pattern for c in "*?[") and fnmatch.fnmatchcase(key, pattern):
                    merged.update(opts)
            merged.update(self.formats.get(key, {}))  # the exact full key is the most specific
        unknown = set(merged) - set(FORMAT_OPTIONS)
        if unknown:
            raise TauraError(f"{ARTIFACTS_FILE}: unknown format option(s) {', '.join(sorted(unknown))}; "
                             f"known: {', '.join(FORMAT_OPTIONS)}")
        return Format(**merged)

    def all_keys(self) -> dict[str, tuple[str, str, str | None, object]]:
        """Every \\res key the code repo's results define: key -> (result, metric, stat, value)."""
        return {k: (name, m, st, v) for name, r in self.results.items() for k, m, st, v in flatten(name, r)}


def load_results(code: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    rdir = code / RESULTS_DIR
    if not rdir.is_dir():
        return out
    for path in sorted(rdir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise TauraError(f"invalid JSON in {path}: {e}") from None
        name = data.get("name", path.stem) if isinstance(data, dict) else None
        if name != path.stem:
            raise TauraError(f"{path}: `name` must match the file name ({path.stem!r})")
        if not KEY_PART.match(name):
            raise TauraError(f"{path}: result name {name!r} may only use letters, digits, _ . -")
        if not isinstance(data.get("metrics"), dict):
            raise TauraError(f"{path}: needs a `metrics` object")
        out[name] = data
    return out


def flatten(name: str, result: dict) -> list[tuple[str, str, str | None, object]]:
    """(key, metric, stat, value) for every scalar in a result, sorted by key.

    `metrics.f1.mean` becomes `name/f1/mean`; a bare `metrics.time` becomes
    `name/time`. Lists (raw `values`) are kept in the ledger but not exposed.
    """
    rows = []
    for metric, v in result["metrics"].items():
        if not KEY_PART.match(metric):
            raise TauraError(f"result {name}: metric name {metric!r} may only use letters, digits, _ . -")
        if isinstance(v, dict):
            for stat, x in v.items():
                if isinstance(x, (int, float)) and not isinstance(x, bool):
                    if not KEY_PART.match(stat):
                        raise TauraError(f"result {name}: stat {metric}.{stat} has an invalid name")
                    rows.append((f"{name}/{metric}/{stat}", metric, stat, x))
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            rows.append((f"{name}/{metric}", metric, None, v))
    return sorted(rows)
