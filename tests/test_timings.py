"""Timings: how long scripts take, from the run records, as ledger results."""

from __future__ import annotations

import pytest

from taura import runs as runsmod
from taura import timings
from taura.errors import TauraError
from taura.ledger import Ledger


def rec(rid, command, start, end, status="done", exit_code=0):
    return {"id": rid, "status": status, "exit_code": exit_code, "command": command, "commit": "0" * 40,
            "dirty": False, "started": f"2026-10-19T{start}+13:00", "finished": f"2026-10-19T{end}+13:00"}


def test_newest_successful_runs_are_summed(tmp_path):
    code = tmp_path
    (code / "ledger/results").mkdir(parents=True)
    (code / "ledger/artifacts.toml").write_text("")
    runsmod.save(code, rec("old", "python3.11 scripts/01_align.py", "10:00:00", "10:10:00"))
    runsmod.save(code, rec("new", "python3.11 scripts/01_align.py --seed 1", "11:00:00", "11:03:18"))
    runsmod.save(code, rec("failed", "python3.11 scripts/01_align.py", "12:00:00", "12:00:05", "failed", 1))
    runsmod.save(code, rec("bench", "python3 scripts/03_bench.py --device cuda", "11:05:00", "11:05:42"))
    (code / "taura.toml").write_text('[timings.evaluation]\nscripts = ["scripts/01_align.py", "scripts/03_bench.py"]\n'
                                     '[timings.warps]\nscripts = ["scripts/02_warps.py"]\n')
    r = Ledger(code).results
    t = r["timing-evaluation"]
    assert t["metrics"] == {"minutes": 4.0, "seconds": 240.0} and sorted(t["runs"]) == ["bench", "new"]
    assert r["timing-warps"]["metrics"] == {} and "no successful run of scripts/02_warps.py" in r["timing-warps"]["label"]
    (code / "taura.toml").write_text("[timings.bad]\nscripts = []\n")
    with pytest.raises(TauraError, match="needs scripts"):
        timings.declared(code)
