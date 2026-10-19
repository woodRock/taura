"""Read-only scanning of the paper repo's .tex sources."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from .generate import RES_CMD

GENERATED = "generated"

IGNORE = "taura:ignore"  # on a line, suppresses heuristic findings (literal-number, seed-claim, figures)
FIGURE_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".eps")

RES_USE = re.compile(r"\\" + RES_CMD + r"\{([^{}]*)\}")
GRAPHICS = re.compile(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^{}]+)\}")
REFERENCE = re.compile(r"\\(?:input|include|includegraphics|subfile)\s*(?:\[[^\]]*\])?\s*\{([^{}]+)\}")


@dataclass
class Use:
    file: str  # relative to the paper root
    line: int
    key: str

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}"


def strip_comment(line: str) -> str:
    m = re.search(r"(?<!\\)%", line)
    return line[: m.start()] if m else line


def tex_sources(paper: Path) -> list[Path]:
    """Hand-written .tex files: everything except generated/ and hidden dirs."""
    out = []
    for root, dirs, files in os.walk(paper):
        rel = Path(root).relative_to(paper)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and not (rel == Path(".") and d == GENERATED))
        out += [Path(root) / f for f in sorted(files) if f.endswith(".tex")]
    return out


def raw_lines(paper: Path):
    """(file, line number, raw line) over the hand-written sources."""
    for path in tex_sources(paper):
        rel = str(path.relative_to(paper))
        for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            yield rel, i, line


def _lines(paper: Path):
    for rel, i, line in raw_lines(paper):
        yield rel, i, strip_comment(line)


def graphics_uses(paper: Path) -> list[tuple[Use, Path | None, bool]]:
    """(use, resolved file or None, ignored) for every \\includegraphics. Use.key is the path as written."""
    out = []
    for rel, i, raw in raw_lines(paper):
        for m in GRAPHICS.finditer(strip_comment(raw)):
            ref = m.group(1).strip()
            cands = [paper / ref] + [paper / (ref + e) for e in FIGURE_EXTS]
            found = next((c for c in cands if c.is_file()), None)
            out.append((Use(rel, i, ref), found, IGNORE in raw))
    return out


def res_uses(paper: Path) -> list[Use]:
    return [Use(rel, i, m.group(1).strip()) for rel, i, line in _lines(paper) for m in RES_USE.finditer(line)]


def _norm(ref: str) -> str:
    ref = os.path.normpath(ref.strip())
    stem, ext = os.path.splitext(ref)
    return stem if ext in (".tex", ".pdf", ".png", ".jpg", ".pgf", "") else ref


def references(paper: Path) -> set[str]:
    """Normalised (extension-less) paths that the paper \\input's or \\includegraphics's."""
    return {_norm(m.group(1)) for _, _, line in _lines(paper) for m in REFERENCE.finditer(line)}


def is_referenced(rel: str, refs: set[str]) -> bool:
    return _norm(rel) in refs


def generated_files(paper: Path) -> list[str]:
    gdir = paper / GENERATED
    if not gdir.is_dir():
        return []
    return sorted(
        str(p.relative_to(paper))
        for p in gdir.rglob("*")
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(gdir).parts)
    )
