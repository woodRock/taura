"""`taura papers`: every paired code and paper repo under the roots."""

from __future__ import annotations

from pathlib import Path

from .discovery import LOCK_FILE, Pair, _code_for_paper, _papers, code_root, paper_root
from .errors import TauraError
from .ledger import ARTIFACTS_FILE


def all_pairs() -> list[Pair]:
    """Code repos under the code root that have a ledger, each with every paper it feeds; plus paper
    repos under the paper root whose lock names a code repo elsewhere."""
    pairs: list[Pair] = []
    seen_papers: set[Path] = set()
    croot, proot = code_root(), paper_root()
    for code in sorted(p for p in croot.iterdir() if (p / ARTIFACTS_FILE).is_file()) if croot.is_dir() else []:
        try:
            papers = _papers(code)
        except TauraError:
            continue
        for which, paper in papers.items():
            pairs.append(Pair(code, paper if paper.is_dir() else None, paper, which))
            seen_papers.add(paper.resolve())
    for paper in sorted(p for p in proot.iterdir() if (p / LOCK_FILE).is_file()) if proot.is_dir() else []:
        if paper.resolve() in seen_papers:
            continue
        code = _code_for_paper(paper)
        if (code / ARTIFACTS_FILE).is_file():
            pairs.append(Pair(code, paper, paper, None))
    return pairs
