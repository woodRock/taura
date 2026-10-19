"""Run records: `taura run`, `taura record` (as gpu-spooler calls it), linking and the run checks."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from test_e2e import commit_all, git, run, setup_ledger

from taura import runs as runsmod
from taura import tomlio
from taura.cli import main
from taura.hashing import content_hash

TRAIN = r'''
import json, os, sys
cfg = open(sys.argv[sys.argv.index("--config") + 1]).read()
seed = int(sys.argv[sys.argv.index("--seed") + 1])
os.makedirs("results", exist_ok=True)
json.dump({"f1": 0.9 + seed / 1000, "run": os.environ["TAURA_RUN_ID"]}, open("results/out.json", "w"))
sys.exit(int(os.environ.get("TRAIN_EXIT", "0")))
'''


def prepare(env, capsys):
    code, paper = env
    run(capsys, "-C", str(code), "init")
    setup_ledger(code)
    (code / "train.py").write_text(TRAIN)
    (code / "configs").mkdir()
    (code / "configs/a.yaml").write_text("lr: 0.1\n")
    (code / "data/fish").mkdir(parents=True)
    (code / "data/fish/a.csv").write_text("1,2\n")
    (code / "taura.toml").write_text('[data.fish]\npath = "data/fish"\nlabel = "v1"\n')
    (code / ".gitignore").write_text(".taura/\nresults/\n")
    commit_all(code, "train")
    return code, paper


def run_cmd(capsys, code, *cmd) -> tuple[int, dict]:
    """`taura --json run -- cmd`: options go before `--`; everything after it is the command's."""
    capsys.readouterr()
    rc = main(["-C", str(code), "--json", "run", "--", *cmd])
    out = capsys.readouterr().out
    return rc, json.loads(out[out.index("{"):]) if "{" in out else {}


def taura_run(capsys, code, seed="3") -> tuple[int, dict]:
    return run_cmd(capsys, code, sys.executable, "train.py", "--config", "configs/a.yaml", "--seed", seed)


def test_taura_run_records_everything(env, capsys):
    code, _ = prepare(env, capsys)
    rc, rec = taura_run(capsys, code)
    assert rc == 0 and rec["status"] == "done" and rec["exit_code"] == 0
    assert rec["commit"] == git(code, "rev-parse", "HEAD").strip() and rec["dirty"] is False
    assert rec["config_path"] == "configs/a.yaml" and rec["config_hash"].startswith("sha256:")
    assert rec["seed"] == 3 and rec["inputs"] == ["configs/a.yaml", "train.py"]
    assert rec["data"]["fish"]["label"] == "v1" and rec["data"]["fish"]["files"] == 1
    out = code / "results/out.json"
    assert rec["outputs"] == {"results/out.json": content_hash(out)}
    assert json.loads(out.read_text())["run"] == rec["id"]  # the command saw TAURA_RUN_ID
    saved = tomlio.load(code / f"ledger/runs/{rec['id']}.toml")
    assert saved["outputs"] == rec["outputs"] and saved["status"] == "done"
    # the record doesn't make the next run dirty; the data hash is cached and tracks changes
    rc, rec2 = taura_run(capsys, code)
    assert rec2["dirty"] is False and rec2["data"]["fish"]["hash"] == rec["data"]["fish"]["hash"]
    (code / "data/fish/a.csv").write_text("1,3\n")
    rc, rec3 = taura_run(capsys, code)
    assert rec3["data"]["fish"]["hash"] != rec["data"]["fish"]["hash"]
    assert (code / ".taura/data-cache.json").is_file()
    rc, listing = run(capsys, "-C", str(code), "runs")
    assert len(listing["runs"]) == 3


def test_taura_run_passes_the_exit_code_through(env, capsys, monkeypatch):
    code, _ = prepare(env, capsys)
    monkeypatch.setenv("TRAIN_EXIT", "3")
    rc, rec = taura_run(capsys, code)
    assert rc == 3 and rec["status"] == "failed" and rec["exit_code"] == 3
    (code / "train.py").write_text(TRAIN + "\n# edited\n")
    rc, rec = taura_run(capsys, code, seed="1")
    assert rec["dirty"] is True  # an uncommitted change to the code
    rc, r = run_cmd(capsys, code, "no-such-command-xyz")
    assert rc == 127


def test_results_link_to_runs_and_checks(env, capsys, monkeypatch):
    code, paper = prepare(env, capsys)
    rc, rec = taura_run(capsys, code)
    # an exporter turns the run's output into a result: same content hash -> linked
    src = code / "results/out.json"
    mba = json.loads((code / "ledger/results/mba.json").read_text())
    mba["source"] = {"path": "results/out.json", "hash": content_hash(src)}
    (code / "ledger/results/mba.json").write_text(json.dumps(mba))
    commit_all(code, "export")
    assert run(capsys, "-C", str(code), "publish")[0] == 0
    rc, t = run(capsys, "-C", str(code), "trace", "mba/f1/mean")
    [rr] = t["items"][0]["result"]["run_records"]
    assert (rr["id"], rr["link"], rr["seed"], rr["status"]) == (rec["id"], "output hash", 3, "done")
    rc, r = run(capsys, "-C", str(code), "check")
    assert rc == 0, r["findings"]

    # the script changes after the run: the published result is suspect
    (code / "train.py").write_text(TRAIN + "\n# a later change\n")
    commit_all(code, "change the script")
    rc, r = run(capsys, "-C", str(code), "check")
    [f] = [f for f in r["findings"] if f["id"] == "code-changed-since-run"]
    assert "result mba" in f["message"] and "train.py" in f["message"]

    # a result naming a run with no record; a record whose commit doesn't exist
    mba["runs"] = ["2026-01-01-nope"]
    (code / "ledger/results/mba.json").write_text(json.dumps(mba))
    runsmod.save(code, {"id": "ghost", "status": "done", "commit": "0" * 40, "dirty": False})
    rc, r = run(capsys, "-C", str(code), "check")
    ids = {f["id"] for f in r["findings"]}
    assert {"missing-run", "missing-commit"} <= ids


def test_record_as_gpu_spooler_calls_it(env, capsys):
    code, _ = prepare(env, capsys)
    head = git(code, "rev-parse", "HEAD").strip()
    (code / "train.py").write_text(TRAIN + "\n# not committed\n")
    snap = git(code, "stash", "create", "gpu-spooler sync").strip()  # what `task sync` pushes for a dirty tree
    git(code, "checkout", "--", "train.py")

    rid = "2026-10-19-spooler-12"
    cmd = f"{sys.executable} train.py --config configs/a.yaml --seed 7"
    rc, rec = run(capsys, "-C", str(code), "record", rid, "--event", "start", "--command", cmd,
                  "--commit", snap, "--host", "ecs-gpu-02", "--gpus", "0,1", "--spooler-job", "12",
                  "--runner", "gpu-spooler", "--started", "1760839331")
    assert rc == 0 and rec["status"] == "running" and rec["host"] == "ecs-gpu-02"
    assert (rec["commit"], rec["dirty"], rec["base_commit"]) == (snap, True, head)
    assert rec["seed"] == 7 and rec["config_path"] == "configs/a.yaml" and rec["spooler_job"] == 12
    assert rec["started"].startswith("2025-10-19")

    rc, rec = run(capsys, "-C", str(code), "record", rid, "--event", "finish", "--exit-code", "0",
                  "--finished", "1760839400")
    assert rec["status"] == "done" and rec["exit_code"] == 0 and rec["commit"] == snap  # merged, not replaced

    (code / "results").mkdir()
    (code / "results/out.json").write_text('{"f1": 0.907}')  # what `task fetch results` brought back
    rc, rec = run(capsys, "-C", str(code), "record", rid, "--event", "fetch", "--outputs", "results")
    assert rec["outputs"] == {"results/out.json": content_hash(code / "results/out.json")}
    assert Path(code / f"ledger/runs/{rid}.toml").is_file()
    rc, r = run(capsys, "-C", str(code), "record", "bad id!", "--event", "start")
    assert rc == 2
