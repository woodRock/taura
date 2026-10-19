"""Find the (code repo, paper repo) pair from either side.

Convention: ~/Desktop/<name> pairs with ~/overleaf/<name>. Roots are
overridable with TAURA_CODE_ROOT / TAURA_PAPER_ROOT. A taura.toml in the
code repo overrides the paper path; a taura.lock in the paper repo records
which code repo it was published from, so the reverse lookup survives a
renamed Overleaf project.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import NAME, tomlio
from .errors import TauraError

CONFIG_FILE = f"{NAME}.toml"
LOCK_FILE = f"{NAME}.lock"
ENV_CODE_ROOT = f"{NAME.upper()}_CODE_ROOT"
ENV_PAPER_ROOT = f"{NAME.upper()}_PAPER_ROOT"


def code_root() -> Path:
    return Path(os.environ.get(ENV_CODE_ROOT, "~/Desktop")).expanduser().resolve()


def paper_root() -> Path:
    return Path(os.environ.get(ENV_PAPER_ROOT, "~/overleaf")).expanduser().resolve()


def contract(path: Path) -> str:
    """Render a path with ~ for the home directory (portable in lock files and headers)."""
    home = Path.home().resolve()
    try:
        return "~/" + str(path.resolve().relative_to(home))
    except ValueError:
        return str(path)


def _expand(p: str, base: Path) -> Path:
    path = Path(p).expanduser()
    return (path if path.is_absolute() else base / path).resolve()


@dataclass
class Pair:
    code: Path
    paper: Path | None  # None if the paper repo doesn't exist yet
    paper_tried: Path  # where we looked, for error messages
    paper_name: str | None = None  # key under [papers.*] when one code repo feeds several

    def require_paper(self) -> Path:
        if self.paper is None:
            raise TauraError(
                f"paper repo not found: tried {self.paper_tried} "
                f"(set `paper` in {self.code / CONFIG_FILE} if the Overleaf project has another name)"
            )
        return self.paper


def _repo_top(start: Path, root: Path) -> Path | None:
    """The direct child of `root` that contains `start`, if any."""
    try:
        rel = start.relative_to(root)
    except ValueError:
        return None
    return root / rel.parts[0] if rel.parts else None


def _papers(code: Path) -> dict[str | None, Path]:
    """All papers this code repo feeds, keyed by [papers.*] name (None for the single default)."""
    cfg_path = code / CONFIG_FILE
    cfg = tomlio.load(cfg_path) if cfg_path.is_file() else {}
    if cfg.get("papers"):
        try:
            return {k: _expand(v["path"], code) for k, v in cfg["papers"].items()}
        except (KeyError, TypeError):
            raise TauraError(f"each [papers.<name>] in {cfg_path} needs a `path`") from None
    if "paper" in cfg:
        return {None: _expand(cfg["paper"], code)}
    return {None: paper_root() / code.name}


def _paper_for_code(code: Path, which: str | None) -> tuple[Path, str | None]:
    papers = _papers(code)
    if None in papers:
        if which is not None:
            raise TauraError(f"--paper {which} given but {code / CONFIG_FILE} has no [papers] table")
        return papers[None], None
    if which is None:
        if len(papers) > 1:
            raise TauraError(
                f"{code / CONFIG_FILE} lists several papers ({', '.join(sorted(papers))}); choose one with --paper"
            )
        which = next(iter(papers))
    if which not in papers:
        raise TauraError(f"no paper {which!r} in {code / CONFIG_FILE}; known: {', '.join(sorted(papers))}")
    return papers[which], which


def _code_for_paper(paper: Path) -> Path:
    lock = paper / LOCK_FILE
    if lock.is_file():
        recorded = tomlio.load(lock).get("code_repo")
        if recorded:
            return _expand(recorded, paper)
    return code_root() / paper.name


def discover(cwd: Path | None = None, paper: str | None = None) -> Pair:
    cwd = (cwd or Path.cwd()).resolve()
    croot, proot = code_root(), paper_root()

    # Check the paper side first: if the roots nest (unusual), the more specific wins.
    roots = sorted([("paper", proot), ("code", croot)], key=lambda r: -len(r[1].parts))
    for side, root in roots:
        top = _repo_top(cwd, root)
        if top is None:
            continue
        if side == "code":
            paper_path, which = _paper_for_code(top, paper)
            return Pair(top, paper_path if paper_path.is_dir() else None, paper_path, which)
        code = _code_for_paper(top)
        if not code.is_dir():
            raise TauraError(f"code repo not found for paper {top}: tried {code}")
        matches = [k for k, p in _papers(code).items() if p == top]
        if not matches:
            raise TauraError(f"{top} looks like a paper repo, but code repo {code} doesn't pair with it")
        if paper is not None and paper not in matches:
            raise TauraError(f"--paper {paper} doesn't point at {top}")
        return Pair(code, top, top, matches[0])

    raise TauraError(
        f"{cwd} is not inside a code repo under {croot} or a paper repo under {proot} "
        f"(override the roots with {ENV_CODE_ROOT} / {ENV_PAPER_ROOT})"
    )
