"""Settings (hyperparameters) as ledger results, so the paper can quote them with \\res{} and adopt can
check typed ones against what the code actually uses.

Declared per repo in taura.toml, wherever the settings live:

    [settings.finetune]
    path = "configs/finetune.yaml"           # YAML (needs PyYAML), TOML or JSON: every number

    [settings.align]
    path = "scripts/01_multibatch_align.py"  # Python: module-level UPPER_CASE = literal constants and
    names = ["N_FOLDS", "RT_BLOCK"]          # argparse defaults, read with ast (never run); names
                                             # restricts the constants (default: all)

Each becomes a result named settings-<name>. Nested keys are joined with dots
(settings-finetune/task.cv_folds); a list of numbers gives count, min and max
(settings-finetune/task.cv_seeds/count); argparse defaults are named after their flag without
dashes (settings-align/n_boot). Literals inside functions are not found: quote those by hand.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from . import tomlio
from .discovery import CONFIG_FILE
from .errors import TauraError
from .hashing import sha256_file

PREFIX = "settings-"
SAFE = re.compile(r"[^A-Za-z0-9_.\-]+")


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def flatten(data, prefix: str = "") -> dict:
    """Numbers (and lists of numbers) of a nested mapping, as {dotted.name: value or {stat: value}}."""
    out: dict = {}
    if isinstance(data, dict):
        for k, v in data.items():
            name = SAFE.sub("-", f"{prefix}.{k}" if prefix else str(k)).strip("-.")
            if name:
                out |= flatten(v, name)
    elif _number(data) and prefix:
        out[prefix] = data
    elif isinstance(data, (list, tuple)) and prefix and data and all(_number(x) for x in data):
        out[prefix] = {"count": len(data), "min": min(data), "max": max(data)}
    return out


def _config(path: Path) -> dict:
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError:
            raise TauraError(f"{path}: reading YAML settings needs PyYAML (pipx inject taura pyyaml, "
                             "or pip install pyyaml)") from None
        return yaml.safe_load(path.read_text()) or {}
    if suffix == ".toml":
        return tomlio.load(path)
    if suffix == ".json":
        return json.loads(path.read_text())
    raise TauraError(f"{path}: settings must be YAML, TOML, JSON or a Python file")


def python_settings(path: Path, names: list[str] | None = None) -> dict:
    """Module-level UPPER_CASE constants and argparse defaults of a Python file, without running it."""
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError as e:
        raise TauraError(f"{path}: {e}") from None
    found: dict = {}
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for t in targets:
            if isinstance(t, ast.Name) and t.id.isupper() and getattr(node, "value", None) is not None:
                try:
                    found[t.id] = ast.literal_eval(node.value)
                except (ValueError, SyntaxError):
                    pass
    if names is not None:
        missing = [n for n in names if n not in found]
        if missing:
            raise TauraError(f"{path}: no module-level literal constant {', '.join(missing)}")
        found = {n: found[n] for n in names}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"
                and node.args and isinstance(node.args[0], ast.Constant) and str(node.args[0].value).startswith("--")):
            default = next((kw.value for kw in node.keywords if kw.arg == "default"), None)
            if default is not None:
                try:
                    found[str(node.args[0].value).lstrip("-").replace("-", "_")] = ast.literal_eval(default)
                except (ValueError, SyntaxError):
                    pass
    return flatten(found)


def declared(code: Path) -> dict[str, dict]:
    cfg_path = code / CONFIG_FILE
    cfg = tomlio.load(cfg_path) if cfg_path.is_file() else {}
    out = {}
    for name, spec in cfg.get("settings", {}).items():
        if not isinstance(spec, dict) or "path" not in spec:
            raise TauraError(f"{cfg_path}: [settings.{name}] needs a `path`")
        out[name] = spec
    return out


def load(code: Path) -> dict[str, dict]:
    """{settings-<name>: result} for every [settings.<name>] in taura.toml."""
    out = {}
    for name, spec in declared(code).items():
        path = code / spec["path"]
        if not path.is_file():
            raise TauraError(f"{CONFIG_FILE}: [settings.{name}] path {spec['path']} not found")
        if path.suffix == ".py":
            metrics = python_settings(path, spec.get("names"))
        else:
            metrics = flatten(_config(path))
        out[PREFIX + name] = {
            "name": PREFIX + name, "label": f"Settings in {spec['path']}", "kind": "settings",
            "metrics": metrics, "source": {"path": spec["path"], "hash": sha256_file(path)},
        }
    return out
