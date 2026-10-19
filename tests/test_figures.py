"""Figures copied from the code repo's own pipeline, PDF content hashing, and prose checks end to end."""

from __future__ import annotations

import os
import time
from pathlib import Path

from test_e2e import ARTIFACTS, MAIN_TEX, commit_all, git, run, setup_ledger

from taura.hashing import content_hash, sha256_file


def write_pdf(path: Path, width: float, date: str) -> None:
    """A minimal valid PDF with an Info dict, like matplotlib writes (date changes every save)."""
    content = f"0 0 1 rg 10 40 {width:.2f} 20 re f".encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R /Resources << >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Creator (Matplotlib v3.10) /Producer (Matplotlib pdf backend) /CreationDate (D:" + date.encode() + b") >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % off for off in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R /Info 5 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out)


def test_content_hash_ignores_metadata(tmp_path):
    a, b, c = tmp_path / "a.pdf", tmp_path / "b.pdf", tmp_path / "c.pdf"
    write_pdf(a, 50, "20261014134752+13'00'")
    write_pdf(b, 50, "20261017150344+13'00'0")  # different date, different length: offsets move
    write_pdf(c, 51, "20261014134752+13'00'")
    assert sha256_file(a) != sha256_file(b) and content_hash(a) == content_hash(b)
    assert content_hash(a) != content_hash(c)


SOURCE_FIG = """
[artifacts.fig_recovery]
kind = "figure"
output = "generated/fig_recovery.pdf"
source = "figs/paper/fig_recovery.pdf"
made_by = "scripts/paper_figures.py"
"""


def test_source_figure_lifecycle(env, capsys):
    code, paper = env
    run(capsys, "-C", str(code), "init")
    setup_ledger(code)
    write_pdf(code / "figs/paper/fig_recovery.pdf", 50, "20261014134752+13'00'")
    (code / "scripts").mkdir()
    (code / "scripts/paper_figures.py").write_text("# writes figs/paper/*.pdf\n")
    past = time.time() - 100
    os.utime(code / "scripts/paper_figures.py", (past, past))
    commit_all(code, "figure pipeline")
    assert run(capsys, "-C", str(code), "publish")[0] == 0

    # a hand-placed figure in the paper: no provenance, but taura finds its twin in the code repo
    write_pdf(paper / "fig_recovery.pdf", 50, "20261001000000+13'00'")
    tex = MAIN_TEX.replace("%EXTRA%", r"\includegraphics{fig_recovery}")
    (paper / "main.tex").write_text(tex)
    commit_all(paper, "hand-placed figure")
    rc, r = run(capsys, "-C", str(code), "check")
    [f] = [f for f in r["findings"] if f["id"] == "untracked-figure"]
    assert f["location"] == "main.tex:7" and 'source = "figs/paper/fig_recovery.pdf"' in f["fix"]

    # adopt it as a source figure
    (code / "ledger/artifacts.toml").write_text(ARTIFACTS + SOURCE_FIG)
    commit_all(code, "track figure")
    rc, r = run(capsys, "-C", str(code), "check")
    assert {f["id"] for f in r["findings"]} == {"untracked-figure", "stale-artifact"}
    assert run(capsys, "-C", str(code), "publish")[0] == 0
    git(paper, "rm", "-q", "fig_recovery.pdf")
    (paper / "main.tex").write_text(tex.replace("{fig_recovery}", "{generated/fig_recovery}"))
    commit_all(paper, "use generated figure")
    rc, r = run(capsys, "-C", str(code), "check")
    assert rc == 0, r["findings"]

    # re-running the figure script with identical output only changes the date: nothing to publish
    write_pdf(code / "figs/paper/fig_recovery.pdf", 50, "20261019200000+13'00'")
    assert run(capsys, "-C", str(code), "check")[0] == 0
    assert run(capsys, "-C", str(code), "diff")[1]["changes"] == []

    # a real change: stale, and diff says so
    write_pdf(code / "figs/paper/fig_recovery.pdf", 60, "20261019200100+13'00'")
    rc, r = run(capsys, "-C", str(code), "check")
    assert [f["id"] for f in r["findings"]] == ["stale-artifact"]
    assert [c["text"] for c in run(capsys, "-C", str(code), "diff")[1]["changes"]] == ["fig_recovery: regenerated"]

    # the script changed after the figure was made
    (code / "scripts/paper_figures.py").write_text("# writes figs/paper/*.pdf, now bigger\n")
    rc, r = run(capsys, "-C", str(code), "check")
    ids = [f["id"] for f in r["findings"]]
    assert "stale-source" in ids
    # reviewed: this version of the script doesn't change the figure -> quiet until it changes again
    [f] = [f for f in r["findings"] if f["id"] == "stale-source"]
    checked = f["fix"].split('checked_against = "')[1].split('"')[0]
    (code / "ledger/artifacts.toml").write_text((code / "ledger/artifacts.toml").read_text().replace(
        'made_by = "scripts/paper_figures.py"', f'made_by = "scripts/paper_figures.py"\nchecked_against = "{checked}"'))
    rc, r = run(capsys, "-C", str(code), "check")
    assert "stale-source" not in [f["id"] for f in r["findings"]]
    (code / "scripts/paper_figures.py").write_text("# changed again\n")
    rc, r = run(capsys, "-C", str(code), "check")
    assert "stale-source" in [f["id"] for f in r["findings"]]


def test_figure_hand_edit_and_missing(env, capsys):
    code, paper = env
    run(capsys, "-C", str(code), "init")
    setup_ledger(code)
    assert run(capsys, "-C", str(code), "publish")[0] == 0
    write_pdf(paper / "generated/fig_alignment.pdf", 99, "20261019000000+13'00'")  # replaced on Overleaf
    (paper / "main.tex").write_text(MAIN_TEX.replace("%EXTRA%", r"\includegraphics{figs/nope}"))
    commit_all(paper, "collaborator")
    rc, r = run(capsys, "-C", str(code), "check")
    found = {(f["id"], f["location"]) for f in r["findings"]}
    assert ("hand-edit", "generated/fig_alignment.pdf") in found
    assert ("missing-figure", "main.tex:7") in found


def test_prose_checks_and_adopt(env, capsys):
    code, paper = env
    run(capsys, "-C", str(code), "init")
    setup_ledger(code)
    assert run(capsys, "-C", str(code), "publish")[0] == 0
    prose = "Over 5 seeds \\res{mba/f1/mean} beats 0.871 (Baseline). Ignore 0.871 here. % taura:ignore"
    (paper / "main.tex").write_text(MAIN_TEX.replace("%EXTRA%", prose))
    commit_all(paper, "typed number")
    rc, r = run(capsys, "-C", str(code), "check")
    assert rc == 0 and r["findings"] == []  # the whole line is ignored
    (paper / "main.tex").write_text(MAIN_TEX.replace("%EXTRA%", prose.split(" Ignore")[0]))
    commit_all(paper, "typed number, not ignored")
    rc, r = run(capsys, "-C", str(code), "check")
    by_id = {f["id"]: f for f in r["findings"]}
    assert set(by_id) == {"literal-number", "seed-claim"}
    assert r"\res{baselines/f1/mean}" in by_id["literal-number"]["fix"]
    assert "n = 3" in by_id["seed-claim"]["message"]
    rc, r = run(capsys, "-C", str(code), "adopt")
    assert rc == 1 and r["summary"]["unique"] == 1
    [lit] = r["literals"]
    assert (lit["location"], lit["suggestion"]) == ("main.tex:7", r"\res{baselines/f1/mean}")


def test_raster_source_figures(env, capsys):
    """An existing PNG the code repo writes can be adopted as is; a generated figure is still PDF."""
    code, paper = env
    run(capsys, "-C", str(code), "init")
    setup_ledger(code)
    (code / "figs").mkdir()
    (code / "figs/saliency.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"pixels")
    toml = (code / "ledger/artifacts.toml").read_text()
    (code / "ledger/artifacts.toml").write_text(toml + '\n[artifacts.saliency]\nkind = "figure"\n'
                                                'output = "generated/saliency.png"\nsource = "figs/saliency.png"\n')
    commit_all(code, "png figure")
    assert run(capsys, "-C", str(code), "publish")[0] == 0
    assert (paper / "generated/saliency.png").read_bytes().endswith(b"pixels")
    (code / "ledger/artifacts.toml").write_text(toml + '\n[artifacts.bad]\nkind = "figure"\n'
                                                'output = "generated/bad.pdf"\nsource = "figs/saliency.png"\n')
    rc, r = run(capsys, "-C", str(code), "build")
    assert rc == 2 and "same extension" in r["error"]
