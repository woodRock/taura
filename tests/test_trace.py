"""`taura trace` and `taura papers`, and the project model behind them (and the TUI)."""

from __future__ import annotations

import json

from test_e2e import MAIN_TEX, commit_all, run, set_result, setup_ledger

from taura.discovery import discover
from taura.model import Project


def published(env, capsys):
    code, paper = env
    run(capsys, "-C", str(code), "init")
    setup_ledger(code)
    assert run(capsys, "-C", str(code), "publish")[0] == 0
    return code, paper


def test_trace_key_location_and_artifact(env, capsys):
    code, paper = published(env, capsys)
    rc, t = run(capsys, "-C", str(code), "trace", "res:mba/f1/mean")
    assert rc == 0 and t["kind"] == "key"
    [c] = t["items"]
    assert (c["location"], c["printed"], c["current"], c["status"]) == ("main.tex:6", "0.912", "0.912", "ok")
    assert c["result"]["key"] == "mba/f1/mean" and c["result"]["value"] == 0.912
    assert c["result"]["file"] == "ledger/results/mba.json" and not c["result"]["changed_since_publish"]
    assert c["artifact"] == {"name": "results", "output": "generated/results.tex"}
    assert c["publish"]["code_commit"]

    rc, t = run(capsys, "-C", str(paper), "trace", "main.tex:6")  # from the paper side
    assert {i["key"] for i in t["items"]} == {"mba/f1/mean", "mba/f1/std", "mba/f1/n"}

    rc, t = run(capsys, "-C", str(code), "trace", "table_main")
    [a] = t["items"]
    assert a["kind"] == "artifact" and a["used_at"] == ["main.tex:10"] and a["status"] == "ok"
    assert [r["name"] for r in a["inputs"]] == ["mba", "baselines"]

    rc, t = run(capsys, "-C", str(code), "trace", "fig_alignment")
    assert t["items"][0]["made"]["script"] == "artifacts/fig_alignment.py"


def test_trace_shows_staleness_and_text(env, capsys):
    code, paper = published(env, capsys)
    set_result(code, "mba", f1=0.917)
    rc, t = run(capsys, "-C", str(code), "trace", "mba/f1/mean")
    [c] = t["items"]
    assert (c["printed"], c["current"], c["status"]) == ("0.912", "0.917", "stale")
    assert c["result"]["changed_since_publish"]
    capsys.readouterr()
    from taura.cli import main
    assert main(["-C", str(code), "trace", "mba/f1/mean"]) == 0
    text = capsys.readouterr().out
    assert "prints 0.912  [stale, code now gives 0.917]" in text and "CHANGED since publish" in text
    assert "runs     none recorded" in text


def test_trace_typed_numbers_and_errors(env, capsys):
    code, paper = published(env, capsys)
    (paper / "main.tex").write_text(MAIN_TEX.replace("%EXTRA%", "Baseline reaches 0.871."))
    commit_all(paper, "typed")
    rc, t = run(capsys, "-C", str(code), "trace", "main.tex:7")
    [typed] = t["items"]
    assert (typed["kind"], typed["number"], typed["verdict"]) == ("typed", "0.871", "matches")
    rc, r = run(capsys, "-C", str(code), "trace", "nope")
    assert rc == 2 and "not a \\res key" in r["error"]
    rc, r = run(capsys, "-C", str(code), "trace", "main.tex:999")
    assert rc == 2 and "no such line" in r["error"]


def test_papers_and_summary(env, capsys, tmp_path):
    code, paper = published(env, capsys)
    rc, r = run(capsys, "papers")
    [p] = r["papers"]
    assert (p["name"], p["claims"], p["artifacts"], p["errors"]) == ("demo", 3, 3, 0)
    assert p["last_publish"] and p["typed_undecided"] == 0
    # a code repo without a paper yet still lists
    other = code.parent / "other"
    (other / "ledger").mkdir(parents=True)
    (other / "ledger/artifacts.toml").write_text("")
    rc, r = run(capsys, "papers")
    assert [x["name"] for x in r["papers"]] == ["demo", "other"] and r["papers"][1]["paper"] is None

    proj = Project(discover(code))
    assert [a.name for a in proj.artifacts] == ["fig_alignment", "results", "table_main"]
    assert all(a.published for a in proj.artifacts)
    assert json.dumps(proj.summary())  # serialisable
