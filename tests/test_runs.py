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

    # a later run at the changed commit wrote the same content: the old run is superseded, nothing to warn
    head = git(code, "rev-parse", "HEAD").strip()
    runsmod.save(code, {"id": "2099-01-01-rerun", "status": "done", "exit_code": 0, "commit": head, "dirty": False,
                        "started": "2099-01-01T00:00:00+00:00", "command": "python train.py",
                        "outputs": {"results/out.json": mba["source"]["hash"]}})
    rc, r = run(capsys, "-C", str(code), "check")
    assert not [f for f in r["findings"] if f["id"] == "code-changed-since-run"], r["findings"]

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
    import socket
    assert rec["data"]["fish"]["hashed_on"] == socket.gethostname()  # not the server's copy
    assert rec["started"].startswith("2025-10-19")

    rc, rec = run(capsys, "-C", str(code), "record", rid, "--event", "finish", "--exit-code", "0",
                  "--finished", "1760839400")
    assert rec["status"] == "done" and rec["exit_code"] == 0 and rec["commit"] == snap  # merged, not replaced

    (code / "results").mkdir()
    (code / "results/out.json").write_text('{"f1": 0.907}')  # what `task fetch results` brought back
    rc, rec = run(capsys, "-C", str(code), "record", rid, "--event", "fetch", "--outputs", "results")
    assert rec["outputs"] == {"results/out.json": content_hash(code / "results/out.json")}
    assert Path(code / f"ledger/runs/{rid}.toml").is_file()
    rc, rec = run(capsys, "-C", str(code), "record", "unknown-1", "--event", "start", "--commit", "unknown",
                  "--host", "cuda06", "--note", "server did not report its checkout")
    assert rec["commit"] == "unknown" and "dirty" not in rec and rec["note"].startswith("server")
    rc, r = run(capsys, "-C", str(code), "record", "bad id!", "--event", "start")
    assert rc == 2


def test_dirty_ignores_taura_files_and_earlier_outputs(env, capsys):
    """A pipeline: step 1 rewrites a tracked report; step 2 must not be recorded as dirty for it."""
    code, _ = prepare(env, capsys)
    (code / ".gitignore").write_text(".taura/\n")  # results/ tracked this time
    (code / "results").mkdir()
    (code / "results/out.json").write_text("{}")
    commit_all(code, "track results")
    rc, first = taura_run(capsys, code)
    assert first["dirty"] is False
    assert git(code, "status", "--porcelain").strip()  # results/out.json (tracked) changed
    (code / "ledger/results/mba.json").write_text((code / "ledger/results/mba.json").read_text() + " ")
    rc, second = taura_run(capsys, code, seed="4")
    assert second["dirty"] is False, runsmod.uncommitted_code(code)
    (code / "configs/a.yaml").write_text("lr: 0.2\n")  # a real edit
    rc, third = taura_run(capsys, code, seed="5")
    assert third["dirty"] is True and runsmod.uncommitted_code(code) == ["configs/a.yaml"]


def test_provenance_only_change_is_a_warning(env, capsys):
    code, paper = prepare(env, capsys)
    assert run(capsys, "-C", str(code), "publish")[0] == 0
    mba = json.loads((code / "ledger/results/mba.json").read_text())
    mba["source"] = {"path": "results/x.npz", "provenance": ["joint-align @ abc1234"]}  # values unchanged
    (code / "ledger/results/mba.json").write_text(json.dumps(mba))
    commit_all(code, "new provenance")
    rc, r = run(capsys, "-C", str(code), "check")
    ids = {f["id"]: f for f in r["findings"]}
    assert "stale-artifact" not in ids and {"provenance-changed"} == set(ids)
    assert ids["provenance-changed"]["severity"] == "warn" and r["errors"] == 0
    mba["metrics"]["f1"]["mean"] = 0.95  # a real change is still an error
    (code / "ledger/results/mba.json").write_text(json.dumps(mba))
    rc, r = run(capsys, "-C", str(code), "check")
    assert any(f["id"] == "stale-artifact" for f in r["findings"])


def _npy(descr: str, shape: tuple, data: bytes) -> bytes:
    """A .npy file by hand (taura has no numpy dependency)."""
    header = repr({"descr": descr, "fortran_order": False, "shape": shape}).encode("latin1")
    header += b" " * ((64 - (10 + len(header) + 1) % 64) % 64) + b"\n"
    return b"\x93NUMPY\x01\x00" + len(header).to_bytes(2, "little") + header + data


def _npz(path: Path, **arrays: bytes) -> None:
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        for name, npy in arrays.items():
            z.writestr(f"{name}.npy", npy)


def test_npz_content_hash_ignores_text_arrays(tmp_path):
    warps = _npy("<f4", (2,), b"\x00\x00\xc0?\x00\x00\x00@")  # [1.5, 2.0]
    _npz(tmp_path / "a.npz", warps=warps, rev=_npy("<U9", (), "6743cf8 ".encode("utf-32-le")[:36]))
    _npz(tmp_path / "b.npz", warps=warps, rev=_npy("<U12", (), "156cfe2 ab  ".encode("utf-32-le")))
    _npz(tmp_path / "c.npz", warps=_npy("<f4", (2,), b"\x00\x00\xc0?\x00\x00\x40@"), rev=_npy("<U1", (), b"x\0\0\0"))
    a, b, c = (content_hash(tmp_path / f"{n}.npz") for n in "abc")
    assert a == b and a != c and a.startswith("npz-sha256:")
    assert (tmp_path / "a.npz").read_bytes() != (tmp_path / "b.npz").read_bytes()


def test_environment_and_diff(env, capsys, tmp_path):
    code, _ = prepare(env, capsys)
    rc, first = taura_run(capsys, code)
    e = first["env"]
    assert e["python"] == ".".join(map(str, sys.version_info[:3])) and e["hash"].startswith("sha256:")
    assert "packages" in e and isinstance(e["gpus"], list)
    rc, second = taura_run(capsys, code, seed="4")
    rc, d = run(capsys, "-C", str(code), "runs", "--diff", first["id"], second["id"])
    assert d["diff"]["seed"] == [3, 4] and "env" not in d["diff"]
    assert d["diff"]["outputs"]["different"] == ["results/out.json"]

    # a run elsewhere reports its environment (gpu-spooler passes the probe's output)
    other = dict(e, python="0.0.1", gpus=["0, NVIDIA A100, 550.1"],  # a version no runner has
                 packages={**e["packages"], "warpalign": "3.0.1 editable @6743cf8de37a"})
    other.pop("hash")
    (tmp_path / "env.json").write_text(json.dumps(other))
    rc, rec = run(capsys, "-C", str(code), "record", "elsewhere-1", "--event", "start", "--host", "cuda06",
                  "--command", "python3 train.py --seed 3", "--env-json", str(tmp_path / "env.json"))
    assert rec["env"]["hash"] != e["hash"] and rec["env"]["gpus"] == ["0, NVIDIA A100, 550.1"]
    rc, d = run(capsys, "-C", str(code), "runs", "--diff", first["id"], "elsewhere-1")
    assert d["diff"]["env"]["python"][1] == "0.0.1"
    assert d["diff"]["env"]["packages"]["warpalign"][1] == "3.0.1 editable @6743cf8de37a"
    runsmod.save(code, {"id": "old", "status": "done", "commit": first["commit"], "dirty": False})  # no env
    rc, d = run(capsys, "-C", str(code), "runs", "--diff", "old", first["id"])
    assert d["diff"]["env"] == {"recorded": [False, True]}
    rc, t = run(capsys, "-C", str(code), "runs", "--diff", first["id"], "nope")
    assert rc == 2


def test_remote_dirty_paths_only_code_counts(env, capsys):
    """A server checkout where an earlier job rewrote tracked results is not dirty code."""
    code, _ = prepare(env, capsys)
    head = git(code, "rev-parse", "HEAD").strip()
    rc, rec = run(capsys, "-C", str(code), "record", "srv-1", "--event", "start", "--command", "python3 x.py",
                  "--commit", head, "--dirty-count", "3", "--dirty-paths", "results/a.json", "figs/b.png",
                  "results/c.csv")
    assert rec["dirty"] is False and rec["changed_outputs"] == ["figs/b.png", "results/a.json", "results/c.csv"]
    rc, rec = run(capsys, "-C", str(code), "record", "srv-2", "--event", "start", "--command", "python3 x.py",
                  "--commit", head, "--dirty-count", "2", "--dirty-paths", "results/a.json", "configs/a.yaml")
    assert rec["dirty"] is True and rec["changed_code"] == ["configs/a.yaml"]
    rc, rec = run(capsys, "-C", str(code), "record", "srv-3", "--event", "start", "--command", "python3 x.py",
                  "--commit", head, "--dirty-count", "2")  # an older runner: count only
    assert rec["dirty"] is True
