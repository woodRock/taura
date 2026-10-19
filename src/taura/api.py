"""Helpers for artifact scripts. Optional: the protocol is just environment variables.

taura runs each artifact script with:
  TAURA_RESULTS   path to a JSON file {result_name: result} of the artifact's results
  TAURA_OUTPUT    path the script must write (a PDF for figures, a JSON table spec for tables)
  TAURA_OPTIONS   JSON of any extra keys in the artifact's [artifacts.<name>] table
  TAURA_ARTIFACT  the artifact's name
Scripts never write into the paper repo.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import NAME

_P = NAME.upper()


def results() -> dict[str, dict]:
    return json.loads(Path(os.environ[f"{_P}_RESULTS"]).read_text())


def output_path() -> Path:
    return Path(os.environ[f"{_P}_OUTPUT"])


def options() -> dict:
    return json.loads(os.environ.get(f"{_P}_OPTIONS", "{}"))


def write_table(columns: list[dict], rows: list[list]) -> None:
    """columns: [{"label": "Method"}, {"label": "F1", "metric": "f1", "bold": "max"}];
    rows: cells are LaTeX strings, numbers, {"mean": .., "std": ..} or None."""
    output_path().write_text(json.dumps({"columns": columns, "rows": rows}, indent=2))
