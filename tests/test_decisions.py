"""Deciding what typed numbers are (by hand or by an agent CLI), and verifying the rewrite."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from test_e2e import MAIN_TEX, commit_all, run, set_result, setup_ledger

PROSE = r"""The ablation scores 0.912 without anchors.
The full MBA model scores 0.912 with them.
In 2016 we trained for 300 epochs, and fitting took 47.5 seconds.
One run takes 12.25 s."""

# Decides from the sentence, the way an agent reading the text would.
FAKE_AGENT = r'''
import json, sys
items = json.loads(sys.stdin.read().split("Items:\n", 1)[1])
out = []
for it in items:
    s, n = it["sentence"], it["number"]
    keys = [c["key"] for c in it["candidates"]]
    if "ablation" in s:
        d = {"verdict": "result", "key": next(k for k in keys if k.startswith("ablation"))}
    elif "MBA" in s and n == "0.912":
        d = {"verdict": "result", "key": "mba/f1/mean"}
    elif "took" in s and n == "47.5":
        d = {"verdict": "unrecorded"}
    else:
        d = {"verdict": "not-a-result"}
    out.append(d | {"id": it["id"], "reason": f"sentence: {s[:40]}"})
print("Here you go:\n" + json.dumps({"decisions": out}))
'''

BAD_AGENT = r'''
import json, sys
items = json.loads(sys.stdin.read().split("Items:\n", 1)[1])
print(json.dumps({"decisions": [{"id": it["id"], "verdict": "result", "key": "made/up/key", "reason": "guess"}
                                for it in items]}))
'''


def setup(env, capsys, tmp_path) -> tuple[Path, Path]:
    code, paper = env
    run(capsys, "-C", str(code), "init")
    setup_ledger(code)
    (code / "ledger/results/ablation.json").write_text(json.dumps(
        {"name": "ablation", "metrics": {"f1": {"mean": 0.9118, "n": 3}}}))
    toml = (code / "ledger/artifacts.toml").read_text()
    (code / "ledger/artifacts.toml").write_text(toml.replace('results = ["baselines", "mba"]',
                                                             'results = ["ablation", "baselines", "mba"]'))
    commit_all(code, "ablation")
    assert run(capsys, "-C", str(code), "publish")[0] == 0
    (paper / "main.tex").write_text(MAIN_TEX.replace("%EXTRA%", PROSE))
    commit_all(paper, "prose")
    (tmp_path / "agent.py").write_text(FAKE_AGENT)
    (tmp_path / "bad_agent.py").write_text(BAD_AGENT)
    return code, paper


def todo(capsys, code) -> dict[str, dict]:
    rc, r = run(capsys, "-C", str(code), "adopt", "--todo")
    return {(l["line"], l["text"]): l for l in r["literals"]}


def test_agent_decides_from_context(env, capsys, tmp_path):
    code, paper = setup(env, capsys, tmp_path)
    items = todo(capsys, code)
    tie = items[(7, "0.912")]
    assert tie["candidates"] == ["ablation/f1/mean", "mba/f1/mean"]
    assert tie["sentence"] == "The ablation scores 0.912 without anchors."
    assert {o["result_label"] for o in tie["options"]} == {"ablation", "mba"}
    assert {k[1] for k in items} == {"0.912", "2016", "300", "47.5", "12.25"}  # strong unique: none here

    rc, r = run(capsys, "-C", str(code), "adopt", "--agent", f"{sys.executable} {tmp_path / 'agent.py'}")
    assert r["problems"] == [] and r["summary"]["decided"] == {"result": 2, "not-a-result": 3, "unrecorded": 1}
    decided = {(l["line"], l["text"]): l["decision"] for l in r["literals"]}
    assert decided[(7, "0.912")]["key"] == "ablation/f1/mean"
    assert decided[(8, "0.912")]["key"] == "mba/f1/mean"
    assert decided[(8, "0.912")]["by"] == Path(sys.executable).name
    assert todo(capsys, code) == {}  # nothing left to decide; a second --agent run would ask nothing
    assert "[decisions." in (code / "ledger/adopt.toml").read_text()

    rc, r = run(capsys, "-C", str(code), "check")
    ids = sorted((f["id"], f["location"]) for f in r["findings"])
    assert ("unrecorded-result", "main.tex:9") in ids
    assert not any(f["location"] == "main.tex:9" and f["id"] == "literal-number" for f in r["findings"])
    assert ("literal-number", "main.tex:7") in ids  # decided, not yet rewritten

    # the agent rewrites the two ties; ids of the remaining numbers survive the edit
    tex = (paper / "main.tex").read_text()
    tex = tex.replace("ablation scores 0.912", r"ablation scores \res{ablation/f1/mean}")
    tex = tex.replace("model scores 0.912", r"model scores \res{mba/f1/mean}")
    (paper / "main.tex").write_text(tex)
    commit_all(paper, "rewrite")
    rc, r = run(capsys, "-C", str(code), "check")
    locs = {(f["id"], f["location"]) for f in r["findings"]}
    assert not {loc for loc in locs if loc[1] in ("main.tex:7", "main.tex:8")}
    assert not any(f["id"] == "adopt-mismatch" for f in r["findings"])


def test_bad_agent_answers_are_rejected(env, capsys, tmp_path):
    code, _ = setup(env, capsys, tmp_path)
    rc, r = run(capsys, "-C", str(code), "adopt", "--agent", f"{sys.executable} {tmp_path / 'bad_agent.py'}")
    assert rc == 1 and sum(r["summary"]["decided"].values()) == 0
    assert all("rejected" in p and "made/up/key" in p for p in r["problems"])
    assert not (code / "ledger/adopt.toml").exists()


def test_resolve_by_hand_and_mismatch(env, capsys, tmp_path):
    code, paper = setup(env, capsys, tmp_path)
    lit = todo(capsys, code)[(10, "12.25")]
    rc, r = run(capsys, "-C", str(code), "resolve", lit["id"], "baselines/time", "--reason", "x")
    assert rc == 2 and "matching keys: mba/time" in r["error"]
    rc, r = run(capsys, "-C", str(code), "resolve", lit["id"], "mba/time")
    assert rc == 2 and "--reason" in r["error"]
    rc, r = run(capsys, "-C", str(code), "resolve", lit["id"], "mba/time", "--reason", "one MBA run", "--by", "me")
    assert rc == 0 and r["decided"][0]["key"] == "mba/time"

    # rewritten: but [format.time] prints 1 decimal, so the paper now says 12.2 where it said 12.25
    (paper / "main.tex").write_text((paper / "main.tex").read_text().replace("12.25 s", r"\res{mba/time} s"))
    commit_all(paper, "rewrite time")
    rc, r = run(capsys, "-C", str(code), "check")
    [m] = [f for f in r["findings"] if f["id"] == "adopt-mismatch"]
    assert "prints 12.2, but the paper said 12.25" in m["message"]

    # a verified rewrite, then a rerun changes the result: the new value is expected, not a mismatch
    (code / "ledger/artifacts.toml").write_text((code / "ledger/artifacts.toml").read_text()
                                                .replace("[format.time]\ndecimals = 1", "[format.time]\ndecimals = 2"))
    commit_all(code, "format fix")
    assert run(capsys, "-C", str(code), "publish")[0] == 0
    rc, r = run(capsys, "-C", str(code), "resolve", "--stamp")
    assert [d["key"] for d in r["decided"]] == ["mba/time"] and r["decided"][0]["result_hash"]
    set_result(code, "mba", time=13.5)
    assert run(capsys, "-C", str(code), "publish")[0] == 0
    rc, r = run(capsys, "-C", str(code), "check")
    assert not any(f["id"] == "adopt-mismatch" for f in r["findings"])

    # a decision for a number already rewritten can still be cleared (by id)
    rc, r = run(capsys, "-C", str(code), "resolve", lit["id"], "--clear")
    assert rc == 0 and lit["id"] not in (code / "ledger/adopt.toml").read_text()
    rc, r = run(capsys, "-C", str(code), "resolve", "--accept-unique")
    assert rc == 0  # nothing strong-unique left; still fine
    rc, r = run(capsys, "-C", str(code), "resolve", "nope")
    assert rc == 2 and "no typed number with id" in r["error"]
