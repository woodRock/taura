"""The user's own settings: where paper repos and code repos live.

`$TAURA_CONFIG`, else `$XDG_CONFIG_HOME/taura/config.toml`, else `~/.config/taura/config.toml`.
The TAURA_PAPER_ROOT / TAURA_CODE_ROOT environment variables still win over it, so a script
or test can point taura elsewhere for one run.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import NAME, tomlio
from .errors import TauraError

# Setting names on the command line, and their keys in the file.
SETTINGS = {"paper-root": "paper_root", "code-root": "code_root"}
DEFAULTS = {"paper_root": "~/overleaf", "code_root": "~/Desktop"}
ENV = {"paper_root": f"{NAME.upper()}_PAPER_ROOT", "code_root": f"{NAME.upper()}_CODE_ROOT"}


def config_path() -> Path:
    if os.environ.get(f"{NAME.upper()}_CONFIG"):
        return Path(os.environ[f"{NAME.upper()}_CONFIG"]).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / NAME / "config.toml"


def read() -> dict:
    path = config_path()
    return tomlio.load(path) if path.is_file() else {}


def root(key: str) -> tuple[Path, str]:
    """A root folder and where it came from: env, config or default."""
    if os.environ.get(ENV[key]):
        return Path(os.environ[ENV[key]]).expanduser().resolve(), "env"
    value = read().get(key)
    if value:
        return Path(str(value)).expanduser().resolve(), "config"
    return Path(DEFAULTS[key]).expanduser().resolve(), "default"


def set_value(key: str, value: str | None) -> Path:
    """Saves a root (None goes back to the default); returns the config file."""
    data = read()
    if value is None:
        data.pop(key, None)
    else:
        folder = Path(value).expanduser()
        if not folder.is_dir():
            raise TauraError(f"{value} is not a folder")
        data[key] = _contract(folder.resolve())
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# {NAME} user settings\n" + tomlio.dumps(data), encoding="utf-8")
    return path


def _contract(path: Path) -> str:
    home = Path.home().resolve()
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)
