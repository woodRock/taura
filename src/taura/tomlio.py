"""TOML reading (stdlib or tomli) and a small deterministic writer."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .errors import TauraError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


def load(path: Path) -> dict:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise TauraError(f"invalid TOML in {path}: {e}") from None


def _key(k: str) -> str:
    if k and all(c.isalnum() or c in "_-" for c in k):
        return k
    return json.dumps(k)


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_key(k)} = {_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"cannot serialise {type(v).__name__} to TOML")


def dumps(data: dict) -> str:
    """Serialise nested dicts. Scalars first, then sub-tables, in insertion order."""
    out: list[str] = []

    def emit(table: dict, prefix: list[str]) -> None:
        table = {k: v for k, v in table.items() if v is not None}  # TOML has no null: absent means unset
        scalars = [(k, v) for k, v in table.items() if not isinstance(v, dict)]
        tables = [(k, v) for k, v in table.items() if isinstance(v, dict)]
        if prefix and (scalars or not tables):
            if out:
                out.append("")
            out.append("[" + ".".join(_key(p) for p in prefix) + "]")
        for k, v in scalars:
            out.append(f"{_key(k)} = {_value(v)}")
        for k, v in tables:
            emit(v, prefix + [k])

    emit(data, [])
    return "\n".join(out) + "\n"
