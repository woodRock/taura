"""End-to-end acceptance tests (handover §10, phase 1).

Builds a throwaway code repo and paper repo as real git repos under a temp
dir, wired together through TAURA_CODE_ROOT / TAURA_PAPER_ROOT.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from taura.cli import main

NAME = "demo"

ARTIFACTS = """\
[artifacts.results]
kind = "macros"
output = "generated/results.tex"
results = ["baselines", "mba"]

[artifacts.table_main]
kind = "table"
output = "generated/table_main.tex"
results = ["mba", "baselines"]
row_header = "Method"
labels = { mba = "MBA", baselines = "Baseline" }
columns = [{ metric = "f1", label = "F1" }, { metric = "time", label = "Time (s)", bold = "min" }]
bold = "max"

[artifacts.fig_alignment]
kind = "figure"
output = "generated/fig_alignment.pdf"
script = "artifacts/fig_alignment.py"
results = ["mba"]

[format.default]
decimals = 3

[format.time]
decimals = 1
"""

# A dependency-free figure script: a tiny valid PDF whose bar length is the F1 mean.
FIG_SCRIPT = r'''
import json, os
res = json.load(open(os.environ["TAURA_RESULTS"]))
w = 10 + 80 * res["mba"]["metrics"]["f1"]["mean"]
content = f"0 0 1 rg 10 40 {w:.2f} 20 re f".encode()
objs = [
    b"<< /Type /Catalog /Pages 2 0 R >>",
    b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R /Resources << >> >>",
    b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
]
out, offsets = b"%PDF-1.4\n", []
for i, o in enumerate(objs, 1):
    offsets.append(len(out))
    out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
xref = len(out)
out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
open(os.environ["TAURA_OUTPUT"], "wb").write(out)
'''

MAIN_TEX = r"""\documentclass{article}
\usepackage{booktabs}
\usepackage{graphicx}
\input{generated/results}
\begin{document}
MBA reaches an F1 of \res{mba/f1/mean}$\pm$\res{mba/f1/std} over \res{mba/f1/n} seeds.
%EXTRA%

\begin{table}[h]\centering
\input{generated/table_main}
\end{table}

\includegraphics[width=3cm]{generated/fig_alignment}
\end{document}
"""


def result(name, f1, std, time):
    return {
        "name": name,
        "runs": [f"2026-10-19-{name}-{i}" for i in range(3)],
        "metrics": {"f1": {"mean": f1, "std": std, "n": 3, "values": [f1 - std, f1, f1 + std]}, "time": time},
        "produced_by": "scripts/aggregate.py",
        "created": "2026-10-19T15:00:00+13:00",
    }


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def commit_all(repo: Path, msg: str) -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)


def run(capsys, *args) -> tuple[int, dict]:
    capsys.readouterr()
    rc = main([*args, "--json"])
    out = capsys.readouterr().out
    return rc, json.loads(out) if out.strip() else {}


def setup_ledger(code: Path) -> None:
    (code / "ledger/artifacts.toml").write_text(ARTIFACTS)
    (code / "artifacts/fig_alignment.py").write_text(FIG_SCRIPT)
    for r in (result("mba", 0.912, 0.008, 12.25), result("baselines", 0.871, 0.012, 9.5)):
        (code / f"ledger/results/{r['name']}.json").write_text(json.dumps(r, indent=2))
    commit_all(code, "ledger")


def set_result(code: Path, name: str, **metrics) -> None:
    p = code / f"ledger/results/{name}.json"
    r = json.loads(p.read_text())
    for k, v in metrics.items():
        if isinstance(r["metrics"][k], dict):
            r["metrics"][k]["mean"] = v
        else:
            r["metrics"][k] = v
    p.write_text(json.dumps(r, indent=2))
    commit_all(code, f"update {name}")


def compile_paper(paper: Path, tmp_path: Path) -> tuple[str, str] | None:
    """(pdf text, log) or None if no LaTeX toolchain."""
    out = tmp_path / "latex-out"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir()
    if shutil.which("latexmk"):
        cmd = ["latexmk", "-pdf", "-interaction=nonstopmode", f"-outdir={out}", "main.tex"]
    elif shutil.which("pdflatex"):
        cmd = ["pdflatex", "-interaction=nonstopmode", f"-output-directory={out}", "main.tex"]
    else:
        return None
    proc = subprocess.run(cmd, cwd=paper, capture_output=True, text=True)
    log = (out / "main.log").read_text(errors="replace")
    assert proc.returncode == 0, log[-3000:]
    if not shutil.which("pdftotext"):
        return "", log
    text = subprocess.run(["pdftotext", str(out / "main.pdf"), "-"], capture_output=True, text=True).stdout
    return text, log


def test_acceptance(env, capsys, tmp_path):
    code, paper = env

    # init is idempotent and scaffolds both sides
    rc, r = run(capsys, "-C", str(code), "init")
    assert rc == 0 and "AGENTS.md" in r["created"] and r["paper_created"] == ["generated/"]
    rc, r = run(capsys, "-C", str(code), "init")
    assert rc == 0 and r["created"] == [] and r["updated"] == []
    setup_ledger(code)

    # 4. build is deterministic
    rc, r = run(capsys, "-C", str(code), "build")
    assert rc == 0
    first = {b["artifact"]: Path(b["staged"]).read_bytes() for b in r["built"]}
    rc, r = run(capsys, "-C", str(code), "build")
    assert {b["artifact"]: Path(b["staged"]).read_bytes() for b in r["built"]} == first
    table = first["table_main"].decode()
    assert table.startswith("% GENERATED BY taura — DO NOT EDIT\n% artifact: table_main\n")
    assert r"MBA & \textbf{0.912 $\pm$ 0.008} & 12.2 \\" in table  # 12.25 -> 12.2 (round-half-even float)
    assert r"Baseline & 0.871 $\pm$ 0.012 & \textbf{9.5} \\" in table

    # publish (works from the paper side too) and the paper is clean
    rc, r = run(capsys, "-C", str(paper), "publish")
    assert rc == 0, r
    assert r["commit"] and "taura publish" in git(paper, "log", "-1", "--format=%B")
    assert git(paper, "status", "--porcelain") == ""
    assert (paper / "taura.lock").is_file()
    rc, r = run(capsys, "-C", str(code), "check")
    assert rc == 0, r["findings"]
    rc, r = run(capsys, "-C", str(code), "publish")
    assert rc == 0 and r["commit"] is None  # nothing changed

    compiled = compile_paper(paper, tmp_path)
    if compiled and compiled[0]:
        assert "0.912" in compiled[0] and "??" not in compiled[0]

    # 3. a dangling key: check reports it and the PDF shows a loud placeholder
    (paper / "main.tex").write_text(MAIN_TEX.replace("%EXTRA%", r"Oops: \res{nope}."))
    commit_all(paper, "collaborator adds a key")
    rc, r = run(capsys, "-C", str(code), "check")
    assert rc == 1
    [f] = [f for f in r["findings"] if f["id"] == "dangling-key"]
    assert f["location"] == "main.tex:7" and f["severity"] == "error"
    compiled = compile_paper(paper, tmp_path)
    if compiled:
        text, log = compiled
        assert "Undefined result key `nope'" in log
        if text:
            assert "??nope??" in text
    (paper / "main.tex").write_text(MAIN_TEX.replace("%EXTRA%", ""))
    commit_all(paper, "remove key")

    # 2. a changed result: stale-artifact, and diff shows the value change
    set_result(code, "mba", f1=0.917)
    rc, r = run(capsys, "-C", str(code), "check")
    assert rc == 1
    stale = {f["location"] for f in r["findings"] if f["id"] == "stale-artifact"}
    assert stale == {"results", "table_main", "fig_alignment"}
    rc, r = run(capsys, "-C", str(code), "diff")
    assert rc == 1
    texts = [c["text"] for c in r["changes"]]
    assert r"results \res{mba/f1/mean}: 0.912 → 0.917" in texts
    assert "table_main row MBA col F1: 0.912 ± 0.008 → 0.917 ± 0.008" in texts
    assert "fig_alignment: regenerated" in texts
    rc, r = run(capsys, "-C", str(code), "publish")
    assert rc == 0 and r["commit"]
    rc, r = run(capsys, "-C", str(code), "check")
    assert rc == 0, r["findings"]

    # 1. a hand edit (as Overleaf would commit it): check flags it, publish refuses
    t = paper / "generated/table_main.tex"
    t.write_text(t.read_text().replace("0.871", "0.875"))
    commit_all(paper, "Overleaf edit")
    rc, r = run(capsys, "-C", str(code), "check")
    assert rc == 1
    [f] = [f for f in r["findings"] if f["id"] == "hand-edit"]
    assert f["location"] == "generated/table_main.tex"
    assert "-Baseline & 0.871" in f["diff"] and "+Baseline & 0.875" in f["diff"]
    before = git(paper, "rev-parse", "HEAD")
    set_result(code, "mba", f1=0.93)
    rc, r = run(capsys, "-C", str(code), "publish")
    assert rc == 1 and "hand" in r["error"]
    assert git(paper, "rev-parse", "HEAD") == before
    assert "0.875" in t.read_text()  # never overwritten


def test_bold_mismatch_and_untracked(env, capsys):
    code, paper = env
    run(capsys, "-C", str(code), "init")
    setup_ledger(code)
    assert run(capsys, "-C", str(code), "publish")[0] == 0

    t = paper / "generated/table_main.tex"
    t.write_text(t.read_text()
                 .replace(r"\textbf{0.912 $\pm$ 0.008}", r"0.912 $\pm$ 0.008")
                 .replace(r"Baseline & 0.871 $\pm$ 0.012", r"Baseline & \textbf{0.871 $\pm$ 0.012}"))
    (paper / "generated/notes.tex").write_text("hand made\n")
    rc, r = run(capsys, "-C", str(paper), "check")
    ids = {f["id"] for f in r["findings"]}
    assert rc == 1 and {"hand-edit", "bold-mismatch", "untracked-generated"} <= ids
    [b] = [f for f in r["findings"] if f["id"] == "bold-mismatch"]
    assert "MBA" in b["message"] and "Baseline" in b["message"]


def test_unused_artifact(env, capsys):
    code, paper = env
    run(capsys, "-C", str(code), "init")
    setup_ledger(code)
    run(capsys, "-C", str(code), "publish")
    (paper / "main.tex").write_text(MAIN_TEX.replace(r"\includegraphics[width=3cm]{generated/fig_alignment}",
                                                     r"% \includegraphics{generated/fig_alignment}"))
    commit_all(paper, "drop figure")
    rc, r = run(capsys, "-C", str(code), "check")
    assert rc == 1
    assert [(f["id"], f["severity"], f["location"]) for f in r["findings"]] == [
        ("unused-artifact", "warn", "generated/fig_alignment.pdf")]


def test_publish_refuses_when_behind_remote(env, capsys, tmp_path):
    code, paper = env
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(remote)], check=True)
    git(paper, "remote", "add", "origin", str(remote))
    git(paper, "push", "-q", "-u", "origin", "master")
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(remote), str(other)], check=True)
    (other / "intro.tex").write_text("New intro.\n")
    commit_all(other, "collaborator")
    git(other, "push", "-q")

    run(capsys, "-C", str(code), "init")
    setup_ledger(code)
    rc, r = run(capsys, "-C", str(code), "publish")
    assert rc == 1 and "1 commit(s) behind origin/master" in r["error"]
    git(paper, "pull", "-q")
    assert run(capsys, "-C", str(code), "publish")[0] == 0
    assert "ahead 1" in git(paper, "status", "-sb")  # committed, never pushed


def test_publish_refuses_dirty_code_and_dirty_paper(env, capsys):
    code, paper = env
    run(capsys, "-C", str(code), "init")
    setup_ledger(code)
    (code / "scratch.py").write_text("x = 1\n")
    rc, r = run(capsys, "-C", str(code), "publish")
    assert rc == 1 and "--allow-dirty-code" in r["error"]
    assert run(capsys, "-C", str(code), "publish", "--allow-dirty-code")[0] == 0
    assert "-dirty" in (paper / "generated/results.tex").read_text().splitlines()[2]
    (paper / "main.tex").write_text("changed")
    rc, r = run(capsys, "-C", str(code), "publish", "--allow-dirty-code")
    assert rc == 1 and "uncommitted" in r["error"]


def test_discovery(env, capsys, tmp_path):
    code, paper = env
    # no pair: clear message naming the path tried
    lonely = code.parent / "lonely"
    lonely.mkdir()
    (lonely / "ledger").mkdir()
    (lonely / "ledger/artifacts.toml").write_text("")
    rc, r = run(capsys, "-C", str(lonely), "publish")
    assert rc == 2 and str(paper.parent / "lonely") in r["error"]
    # check still works without the paper
    rc, r = run(capsys, "-C", str(lonely), "check")
    assert rc == 0 and "code-side checks only" in r["notes"][0]

    # a renamed Overleaf project via taura.toml, found from both sides
    renamed = paper.parent / "Renamed Paper"
    paper.rename(renamed)
    (code / "taura.toml").write_text('paper = "../../overleaf/Renamed Paper"\n')
    run(capsys, "-C", str(code), "init")
    setup_ledger(code)
    assert run(capsys, "-C", str(code), "publish")[0] == 0
    rc, r = run(capsys, "-C", str(renamed / "generated"), "status")
    assert rc == 0 and r["paper"] == str(renamed.resolve())

    # outside both roots
    rc, r = run(capsys, "-C", str(tmp_path), "check")
    assert rc == 2 and "TAURA_CODE_ROOT" in r["error"]


def test_macro_formatting(env, capsys):
    code, paper = env
    run(capsys, "-C", str(code), "init")
    setup_ledger(code)
    toml = (code / "ledger/artifacts.toml").read_text()
    (code / "ledger/artifacts.toml").write_text(toml + '\n[format."mba/f1"]\ndecimals = 1\npercent = true\n')
    rc, r = run(capsys, "-C", str(code), "build", "results")
    body = Path(r["built"][0]["staged"]).read_text()
    assert r"\csname taura@mba/f1/mean\endcsname{91.2}" in body
    assert r"\csname taura@mba/f1/n\endcsname{3}" in body
    assert r"\csname taura@baselines/f1/mean\endcsname{0.871}" in body
    assert r"\csname taura@mba/time\endcsname{12.2}" in body
    assert "values" not in body
