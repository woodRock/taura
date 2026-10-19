"""Typed numbers in prose, matching them to results, seed claims, and number formats."""

from __future__ import annotations

import json

import pytest

from taura.claims import literals, match, seed_claims
from taura.errors import TauraError
from taura.ledger import Format, Ledger

TEX = r"""\documentclass{article}
\setlength{\tabcolsep}{6pt}
\begin{document}
The joint model reaches 0.598 on drifted pairs, using 4,396 anchors.
Coverage is 81.2\% \cite{ref2019} (Fig.~\ref{fig:3}), see \includegraphics[width=0.5\textwidth]{fig2.pdf}.
In sep2015 the drift is 3.2~min; $\rho=-0.40$; T = 200 bins. % 0.598 in a comment
A shaky claim: 0.598 again. % taura:ignore
Averaged over 5 seeds: \res{mba/f1/mean}.
\begin{tabular}{@{}lr>{\raggedright}p{72pt}@{}}
\end{tabular}
\end{document}
"""


@pytest.fixture
def repos(tmp_path):
    code, paper = tmp_path / "code", tmp_path / "paper"
    (code / "ledger/results").mkdir(parents=True)
    (code / "ledger/artifacts.toml").write_text('[format.default]\ndecimals = 3\n')
    results = {
        "joint": {"drifted": {"mean": 0.59847, "n": 6}, "coverage": 0.8121},
        "pairs": {"sep-jul": {"anchors": 4396}},
        "mba": {"f1": {"mean": 0.912, "std": 0.01, "n": 3}},
        "other": {"x": {"mean": 0.5976}},  # rounds to 0.598 too
    }
    for name, metrics in results.items():
        (code / f"ledger/results/{name}.json").write_text(json.dumps({"name": name, "metrics": metrics}))
    paper.mkdir()
    (paper / "main.tex").write_text(TEX)
    return code, paper


def test_literals_skip_markup(repos):
    _, paper = repos
    found = [(l.line, l.text) for l in literals(paper)]
    assert found == [
        (4, "0.598"), (4, "4,396"),
        (5, r"81.2\%"),
        (6, "3.2"), (6, "-0.40"), (6, "200"),
        (8, "5"),
    ]  # not: preamble, \cite, \ref, widths, sep2015, comments, ignored line, tabular colspec


def test_match(repos):
    code, paper = repos
    by_text = {l.text: l for l in match(literals(paper), Ledger(code))}
    assert by_text["0.598"].candidates == ["joint/drifted/mean", "other/x/mean"]
    assert by_text["0.598"].suggestion is None
    anchors = by_text["4,396"]
    assert anchors.suggestion == r"\res{pairs/sep-jul/anchors}" and anchors.renders_as == "4396"
    assert anchors.format_hint == '[format."pairs/sep-jul/anchors"] thousands = true'
    pct = by_text[r"81.2\%"]
    assert pct.candidates == ["joint/coverage"] and pct.strong
    assert "percent = true" in pct.format_hint and "decimals = 1" in pct.format_hint
    assert not by_text["200"].candidates and not by_text["5"].candidates  # small ints never match
    assert not by_text["-0.40"].strong and by_text["-0.40"].negative
    assert not by_text["-0.40"].candidates  # negative number: positive results never match


def test_seed_claim(repos):
    code, paper = repos
    [c] = seed_claims(paper, Ledger(code))
    assert (c.line, c.claimed, c.key, c.actual) == (8, 5, "mba/f1/mean", 3)
    (paper / "main.tex").write_text(TEX.replace("over 5 seeds", "over three seeds"))
    assert seed_claims(paper, Ledger(code)) == []


@pytest.mark.parametrize("fmt,value,stat,want", [
    (Format(decimals=1, percent=True), 0.8121, None, "81.2"),
    (Format(thousands=True), 25011, None, "25{,}011"),
    (Format(thousands=True, decimals=1), 1234.56, None, "1{,}234.6"),
    (Format(sign=True, decimals=4), 0.0969, None, "+0.0969"),
    (Format(sign=True, decimals=2), -0.001, None, "0.00"),
    (Format(sign=True), -0.5, None, "-0.500"),
    (Format(thousands=True), 3, "n", "3"),
    (Format(sci=True, decimals=0, round="up"), 0.00195, None, r"2\times10^{-3}"),
    (Format(sci=True, decimals=0, round="up"), 0.00102, None, r"2\times10^{-3}"),
    (Format(sci=True, decimals=1), 0.000996, None, r"1.0\times10^{-3}"),
    (Format(decimals=2, round="up"), 0.1365, None, "0.14"),
    (Format(decimals=2, round="up"), 0.1301, None, "0.14"),
])
def test_formats(fmt, value, stat, want):
    assert fmt.render(value, stat) == want


def test_format_globs(repos):
    code, _ = repos
    (code / "ledger/artifacts.toml").write_text(
        '[format.default]\ndecimals = 3\n[format."pairs/*/anchors"]\nthousands = true\n'
        '[format.drifted]\ndecimals = 2\n')
    led = Ledger(code)
    assert led.format_for("pairs", "sep-jul", "pairs/sep-jul/anchors").thousands
    assert not led.format_for("joint", "drifted", "joint/drifted/mean").thousands
    assert led.format_for("joint", "drifted", "joint/drifted/mean").decimals == 2
    (code / "ledger/artifacts.toml").write_text('[format.default]\ndigits = 3\n')
    with pytest.raises(TauraError, match="digits"):
        Ledger(code).format_for(None, None)


def test_signs_and_scientific_bounds(tmp_path):
    (tmp_path / "x.tex").write_text("Holm $p\\le2\\times10^{-3}$ over 5--40~min, $\\rho=-0.40$, pre-2015.\n")
    found = {l.text: l for l in literals(tmp_path)}
    assert set(found) == {r"2\times10^{-3}", "5", "40", "-0.40", "2015"}
    p = found[r"2\times10^{-3}"]
    assert (p.value, p.exponent, p.strong) == ("2", -3, False)
    code = tmp_path / "code"
    (code / "ledger/results").mkdir(parents=True)
    (code / "ledger/artifacts.toml").write_text("")
    (code / "ledger/results/w.json").write_text(json.dumps(
        {"name": "w", "metrics": {"holm-p": {"max": 0.00195}, "rho": -0.4012}}))
    by = {l.text: l for l in match(literals(tmp_path), Ledger(code))}
    assert by[r"2\times10^{-3}"].candidates == ["w/holm-p/max"]
    assert "sci = true" in by[r"2\times10^{-3}"].format_hint
    assert by["-0.40"].candidates == ["w/rho"]


def test_lines_outside_document_are_scanned_in_inputs(tmp_path):
    (tmp_path / "chapter.tex").write_text("We report 0.912 here.\n")
    assert [l.text for l in literals(tmp_path)] == ["0.912"]
