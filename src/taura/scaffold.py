"""`taura init`: scaffold the convention into a code repo. Idempotent."""

from __future__ import annotations

from importlib import resources

from .discovery import Pair
from .gitutil import STAGING_DIR
from .ledger import ARTIFACTS_FILE, RESULTS_DIR, RUNS_DIR
from .paper import GENERATED

AGENTS_MARKER = "# Rules for agents in this repo (taura provenance)"

SNIPPET = r"""Add to the preamble of the paper's main .tex file (taura never edits it):

  \usepackage{booktabs}          % generated tables use \toprule/\midrule/\bottomrule
  \input{generated/results}      % defines \res{key}

Then write results in prose as \res{mba/f1/mean}, tables as \input{generated/table_main},
and figures as \includegraphics{generated/fig_alignment}."""


def _template(name: str) -> str:
    return resources.files(__package__).joinpath("templates", name).read_text(encoding="utf-8")


def init(pair: Pair) -> dict:
    code = pair.code
    created, skipped, updated = [], [], []

    def write(rel: str, text: str) -> None:
        p = code / rel
        if p.exists():
            skipped.append(rel)
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        created.append(rel)

    write(f"{RUNS_DIR}/.gitkeep", "")
    write(f"{RESULTS_DIR}/.gitkeep", "")
    write(ARTIFACTS_FILE, _template("artifacts.toml"))
    write("artifacts/README.md", _template("README_artifacts.md"))

    agents = code / "AGENTS.md"
    rules = _template("AGENTS.md")
    if not agents.exists():
        write("AGENTS.md", rules)
    elif AGENTS_MARKER in agents.read_text(encoding="utf-8"):
        skipped.append("AGENTS.md")
    else:
        with open(agents, "a", encoding="utf-8") as f:
            f.write("\n" + rules)
        updated.append("AGENTS.md")

    gi = code / ".gitignore"
    lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    if f"{STAGING_DIR}/" in lines or STAGING_DIR in lines:
        skipped.append(".gitignore")
    else:
        with open(gi, "a", encoding="utf-8") as f:
            f.write(("\n" if lines and lines[-1] else "") + f"# taura build staging\n{STAGING_DIR}/\n")
        (updated if lines else created).append(".gitignore")

    paper_created = []
    if pair.paper is not None:
        g = pair.paper / GENERATED
        if not g.is_dir():
            g.mkdir()
            paper_created.append(f"{GENERATED}/")

    return {
        "code": str(code),
        "paper": str(pair.paper) if pair.paper else None,
        "paper_tried": str(pair.paper_tried),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "paper_created": paper_created,
        "instructions": SNIPPET if pair.paper is not None else None,
    }
