"""Settings (hyperparameters) from configs and code, as ledger results."""

from __future__ import annotations

import json

import pytest
from test_e2e import MAIN_TEX, commit_all, run, setup_ledger

from taura import settings
from taura.claims import literals, match
from taura.errors import TauraError
from taura.ledger import Ledger

SCRIPT = '''
import argparse
LAMBDAS = [0.1, 1.0, 10.0]
RT_BLOCK = 12
N_FOLDS = 5
name = "lowercase is not a constant"
COMPUTED = len(LAMBDAS)  # not a literal: skipped

def main():
    lam = 10  # inside a function: not found
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-boot", type=int, default=200)
    ap.add_argument("--seed", default=0)
    ap.add_argument("--out")
'''


def test_flatten_and_files(tmp_path):
    assert settings.flatten({"task": {"cv_folds": 5, "cv_seeds": [0, 1, 2], "name": "x", "on": True},
                             "lr": 1e-4}) == {
        "task.cv_folds": 5, "task.cv_seeds": {"count": 3, "min": 0, "max": 2}, "lr": 1e-4}
    (tmp_path / "train.py").write_text(SCRIPT)
    found = settings.python_settings(tmp_path / "train.py")
    assert found == {"LAMBDAS": {"count": 3, "min": 0.1, "max": 10.0}, "RT_BLOCK": 12, "N_FOLDS": 5,
                     "n_boot": 200, "seed": 0}
    assert settings.python_settings(tmp_path / "train.py", ["N_FOLDS"]) == {"N_FOLDS": 5, "n_boot": 200, "seed": 0}
    with pytest.raises(TauraError, match="NOPE"):
        settings.python_settings(tmp_path / "train.py", ["NOPE"])


def setup(env, capsys):
    code, paper = env
    run(capsys, "-C", str(code), "init")
    setup_ledger(code)
    (code / "configs").mkdir()
    (code / "configs/finetune.yaml").write_text("task:\n  cv_folds: 5\n  cv_seeds: [0,1,2,3,4,5,6,7,8,9]\n"
                                                "optim:\n  lr: 0.0001\n  batch_size: 32\n")
    (code / "train.py").write_text(SCRIPT)
    (code / "taura.toml").write_text('[settings.finetune]\npath = "configs/finetune.yaml"\n\n'
                                     '[settings.train]\npath = "train.py"\n')
    toml = (code / "ledger/artifacts.toml").read_text()
    (code / "ledger/artifacts.toml").write_text(toml.replace('results = ["baselines", "mba"]', 'results = ["*"]', 1))
    commit_all(code, "settings")
    return code, paper


def test_settings_are_results_and_candidates(env, capsys):
    code, paper = setup(env, capsys)
    led = Ledger(code)
    assert led.results["settings-finetune"]["metrics"]["optim.batch_size"] == 32
    assert led.results["settings-train"]["source"]["path"] == "train.py"
    assert "settings-finetune/task.cv_seeds/count" in led.all_keys()
    (paper / "main.tex").write_text(MAIN_TEX.replace("%EXTRA%", "We use a batch size of 256 and 200 bootstrap refits."))
    by = {l.text: l for l in match(literals(paper), led)}
    assert by["200"].candidates == ["settings-train/n_boot"]
    assert not by["256"].candidates  # typed 256 but configured 32: not a match, so adopt shows it
    commit_all(paper, "typed settings")
    # published like any result: \res{settings-...} works, and a config change makes it stale
    assert run(capsys, "-C", str(code), "publish")[0] == 0
    rc, r = run(capsys, "-C", str(code), "trace", "settings-finetune/optim.batch_size")
    assert r["items"][0]["printed"] == "32"
    (code / "configs/finetune.yaml").write_text((code / "configs/finetune.yaml").read_text().replace("32", "64"))
    commit_all(code, "bigger batch")
    rc, r = run(capsys, "-C", str(code), "check")
    assert any(f["id"] == "stale-artifact" and f["location"] == "results" for f in r["findings"])


def test_clash_and_missing(env, capsys):
    code, _ = setup(env, capsys)
    (code / "ledger/results/settings-train.json").write_text(json.dumps({"name": "settings-train", "metrics": {}}))
    with pytest.raises(TauraError, match="clash"):
        _ = Ledger(code).results
    (code / "ledger/results/settings-train.json").unlink()
    (code / "taura.toml").write_text('[settings.gone]\npath = "configs/nope.yaml"\n')
    with pytest.raises(TauraError, match="not found"):
        _ = Ledger(code).results


def test_redo_asks_again(env, capsys, tmp_path):
    import sys
    code, paper = setup(env, capsys)
    (paper / "main.tex").write_text(MAIN_TEX.replace("%EXTRA%", "We use 200 bootstrap refits."))
    commit_all(paper, "typed")
    lit = next(l for l in run(capsys, "-C", str(code), "adopt", "--all")[1]["literals"] if l["text"] == "200")
    assert run(capsys, "-C", str(code), "resolve", lit["id"], "--none", "--reason", "a setting")[0] == 0
    agent = tmp_path / "agent.py"
    agent.write_text('import json,sys\nitems=json.loads(sys.stdin.read().split("Items:\\n",1)[1])\n'
                     'print(json.dumps({"decisions":[{"id":i["id"],"verdict":"result","key":i["candidates"][0]["key"],'
                     '"reason":"the text states the setting"} for i in items if i["candidates"]]}))\n')
    rc, r = run(capsys, "-C", str(code), "adopt", "--agent", f"{sys.executable} {agent}", "--include-unique")
    assert r["summary"]["decided"]["not-a-result"] == 1  # already decided: not asked again
    rc, r = run(capsys, "-C", str(code), "adopt", "--agent", f"{sys.executable} {agent}", "--include-unique",
                "--redo", "not-a-result")
    decided = {l["text"]: l["decision"] for l in r["literals"] if l["decision"]}
    assert decided["200"]["key"] == "settings-train/n_boot"


MODEL = '''
from dataclasses import dataclass
S_MIN, S_MAX = 0.5, 2.0
GAUGE = 1e3

@dataclass
class TrainConfig:
    steps: int = 1500
    lr: float = 3e-3
    blur: tuple = (2.0, 1.0, 0.5)
    seed: int = 0
    name: str = "x"

class Model:
    def __init__(self, x, d_emb: int = 8, hidden: int = 64, *, act=None):
        pass

    def fit(self, data, steps=200):
        pass

def harvest(study, max_drift_min=20.0, sim_threshold=0.30):
    pass
'''


def test_tuples_class_fields_and_keyword_defaults(tmp_path):
    (tmp_path / "model.py").write_text(MODEL)
    found = settings.python_settings(tmp_path / "model.py")
    assert found == {"S_MIN": 0.5, "S_MAX": 2.0, "GAUGE": 1e3}  # classes and functions only when named
    found = settings.python_settings(tmp_path / "model.py",
                                     ["S_MIN", "TrainConfig", "Model.d_emb", "Model.fit", "harvest.sim_threshold"])
    assert found["S_MIN"] == 0.5 and "S_MAX" not in found
    assert found["TrainConfig.steps"] == 1500 and found["TrainConfig.lr"] == 3e-3
    assert found["TrainConfig.blur"] == {"count": 3, "min": 0.5, "max": 2.0}
    assert "TrainConfig.name" not in found  # strings aren't numbers
    assert found["Model.d_emb"] == 8 and "Model.hidden" not in found and found["Model.fit.steps"] == 200
    assert found["harvest.sim_threshold"] == 0.30 and "harvest.max_drift_min" not in found
    with pytest.raises(TauraError, match="TrainConfig.nope"):
        settings.python_settings(tmp_path / "model.py", ["TrainConfig.nope"])


def test_settings_from_an_installed_module(tmp_path, monkeypatch):
    import sys

    pkg = tmp_path / "site" / "fakebench"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "anchors.py").write_text(MODEL)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "site"))
    code = tmp_path / "code"
    code.mkdir()
    (code / "taura.toml").write_text(f'[settings.match]\nmodule = "fakebench.anchors"\npython = "{sys.executable}"\n'
                                     'names = ["harvest"]\n')
    r = settings.load(code)["settings-match"]
    assert r["metrics"] == {"harvest.max_drift_min": 20.0, "harvest.sim_threshold": 0.30}
    assert r["source"]["module"] == "fakebench.anchors" and r["source"]["hash"].startswith("sha256:")
    (code / "taura.toml").write_text(f'[settings.match]\nmodule = "nope.nothing"\npython = "{sys.executable}"\n')
    with pytest.raises(TauraError, match="can't find the source of nope.nothing"):
        settings.load(code)
