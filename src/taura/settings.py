"""Settings (hyperparameters) as ledger results, so the paper can quote them with \\res{} and adopt can
check typed ones against what the code actually uses.

Declared per repo in taura.toml, wherever the settings live:

    [settings.finetune]
    path = "configs/finetune.yaml"           # YAML (needs PyYAML), TOML or JSON: every number

    [settings.align]
    path = "scripts/01_multibatch_align.py"  # Python: module-level UPPER_CASE = literal constants and
    names = ["N_FOLDS", "RT_BLOCK"]          # argparse defaults, read with ast (never run); names
                                             # restricts the constants (default: all)

    [settings.neural]
    path = "src/pkg/model.py"                # names can also pick classes and functions:
    names = ["TrainConfig", "Model.d_emb"]   # dataclass fields, keyword defaults (of Model.__init__)

    [settings.match]
    module = "alignbench.anchors"            # an installed module (another repo), found with this
    python = "python3.11"                    # interpreter; its file hash is recorded
    names = ["MatchConfig.sim_threshold"]

Each becomes a result named settings-<name>. Nested keys are joined with dots
(settings-finetune/task.cv_folds); a list of numbers gives count, min and max
(settings-finetune/task.cv_seeds/count); argparse defaults are named after their flag without
dashes (settings-align/n_boot). Literals inside functions are not found: quote those by hand.
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
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


def _literal(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return None


def _defaults(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> dict:
    """Keyword defaults of a function that are literals."""
    args = fn.args.posonlyargs + fn.args.args
    pairs = list(zip(args[len(args) - len(fn.args.defaults):], fn.args.defaults))
    pairs += [(a, d) for a, d in zip(fn.args.kwonlyargs, fn.args.kw_defaults) if d is not None]
    return {a.arg: v for a, d in pairs if (v := _literal(d)) is not None}


def _scoped(tree: ast.Module) -> dict:
    """Class fields and function defaults, named Class.field, Class.arg (of __init__),
    Class.method.arg and function.arg. Read only when taura.toml names them."""
    out: dict = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out |= {f"{node.name}.{k}": v for k, v in _defaults(node).items()}
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and item.value is not None:
                    if (v := _literal(item.value)) is not None:
                        out[f"{node.name}.{item.target.id}"] = v
                elif isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
                    if (v := _literal(item.value)) is not None:
                        out[f"{node.name}.{item.targets[0].id}"] = v
                elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    prefix = node.name if item.name == "__init__" else f"{node.name}.{item.name}"
                    out |= {f"{prefix}.{k}": v for k, v in _defaults(item).items()}
    return out


def python_settings(path: Path, names: list[str] | None = None) -> dict:
    """Settings of a Python file, read with ast (never run): module-level UPPER_CASE = literal constants
    (A, B = 1, 2 too) and argparse defaults; with `names`, also classes and functions it names:
    "TrainConfig" (every literal field of a dataclass or class), "TrainConfig.steps" (one), "Model.d_emb"
    (a keyword default of Model.__init__), "fit" or "fit.steps" (keyword defaults of a function)."""
    try:
        tree = ast.parse(path.read_text(), filename=str(path))
    except SyntaxError as e:
        raise TauraError(f"{path}: {e}") from None
    found: dict = {}
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        value = getattr(node, "value", None)
        for t in targets:
            if value is None:
                continue
            if isinstance(t, ast.Name) and t.id.isupper():
                if (v := _literal(value)) is not None:
                    found[t.id] = v
            elif isinstance(t, ast.Tuple) and isinstance(value, ast.Tuple) and len(t.elts) == len(value.elts):
                for el, val in zip(t.elts, value.elts):
                    if isinstance(el, ast.Name) and el.id.isupper() and (v := _literal(val)) is not None:
                        found[el.id] = v
    if names is not None:
        constants = [n for n in names if "." not in n and n.isupper()]
        missing = [n for n in constants if n not in found]
        scoped = _scoped(tree)
        picked: dict = {}
        for sel in (n for n in names if n not in constants):
            hit = {k: v for k, v in scoped.items() if k == sel or k.startswith(sel + ".")}
            if not hit:
                missing.append(sel)
            picked |= hit
        if missing:
            raise TauraError(f"{path}: no module-level literal constant, class field or keyword default "
                             f"{', '.join(missing)}")
        found = {n: found[n] for n in constants} | picked
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument"
                and node.args and isinstance(node.args[0], ast.Constant) and str(node.args[0].value).startswith("--")):
            default = next((kw.value for kw in node.keywords if kw.arg == "default"), None)
            if default is not None and (v := _literal(default)) is not None:
                found[str(node.args[0].value).lstrip("-").replace("-", "_")] = v
    return flatten(found)


def module_file(module: str, python: str = "python3") -> Path:
    """Where an installed module's source is, as the project's interpreter sees it (imports its package)."""
    code = ("import importlib.util, sys; s = importlib.util.find_spec(sys.argv[1]); "
            "print(s.origin if s and s.origin else '')")
    try:
        r = subprocess.run([python, "-c", code, module], capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise TauraError(f"can't run {python} to find {module}: {e}") from None
    origin = r.stdout.strip()
    if r.returncode != 0 or not origin.endswith(".py"):
        raise TauraError(f"{python} can't find the source of {module} (is it installed there?)")
    return Path(origin)


def declared(code: Path) -> dict[str, dict]:
    cfg_path = code / CONFIG_FILE
    cfg = tomlio.load(cfg_path) if cfg_path.is_file() else {}
    out = {}
    for name, spec in cfg.get("settings", {}).items():
        if not isinstance(spec, dict) or ("path" in spec) == ("module" in spec):
            raise TauraError(f"{cfg_path}: [settings.{name}] needs a `path` (in this repo) or a `module` (installed)")
        out[name] = spec
    return out


def load(code: Path) -> dict[str, dict]:
    """{settings-<name>: result} for every [settings.<name>] in taura.toml."""
    out = {}
    for name, spec in declared(code).items():
        if "module" in spec:
            path = module_file(spec["module"], spec.get("python", "python3"))
            where = spec["module"]
        else:
            path, where = code / spec["path"], spec["path"]
            if not path.is_file():
                raise TauraError(f"{CONFIG_FILE}: [settings.{name}] path {spec['path']} not found")
        if path.suffix == ".py":
            metrics = python_settings(path, spec.get("names"))
        else:
            metrics = flatten(_config(path))
        source = {"path": where, "hash": sha256_file(path)} | ({"module": spec["module"]} if "module" in spec else {})
        out[PREFIX + name] = {
            "name": PREFIX + name, "label": f"Settings in {where}", "kind": "settings",
            "metrics": metrics, "source": source,
        }
    return out
