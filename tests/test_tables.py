"""Table layout: rules, column specs, row-wise bold, group rows (the joint-align Table II shape)."""

from __future__ import annotations

import pytest

from taura.checks import check_bold
from taura.errors import TauraError
from taura.generate import parse_table, render_header, render_table
from taura.ledger import Format

SPEC = {
    "columns": [
        {"label": r"$A\times B$"},
        {"label": "Anchors", "metric": "anchors", "bold": "none"},
        {"label": "Unal.", "metric": "ticr"},
        {"label": "Joint", "metric": "ticr"},
        {"label": "Joint-CF", "metric": "ticr", "bold": "none"},
    ],
    "rows": [
        {"group": r"\textit{Drifted}"},
        [r"sep15 $\times$ jul16", 4396, 0.054, 0.615, 0.6101],
        [r"apr16 $\times$ jul16", 1463, 0.898, 0.9024, 0.9019],
        {"rule": True},
        ["Mean", "", 0.476, 0.7587, 0.756],
    ],
}
OPTS = {"rules": "hline", "colspec": "@{}lrrrr@{}", "bold": "max", "bold_by": "row", "group_align": "@{}l"}


def fmt(metric):
    return Format(decimals=3)


def test_row_bold_hline_groups():
    body, bold, bold_by = render_table(SPEC, fmt, OPTS)
    assert body.splitlines() == [
        r"\begin{tabular}{@{}lrrrr@{}}",
        r"\hline",
        r"$A\times B$ & Anchors & Unal. & Joint & Joint-CF \\",
        r"\hline",
        r"\multicolumn{5}{@{}l}{\textit{Drifted}} \\",
        r"sep15 $\times$ jul16 & 4396 & 0.054 & \textbf{0.615} & 0.610 \\",
        # Joint-CF ties Joint at the printed precision but is excluded from bolding
        r"apr16 $\times$ jul16 & 1463 & 0.898 & \textbf{0.902} & 0.902 \\",
        r"\hline",
        r"Mean &  & 0.476 & \textbf{0.759} & 0.756 \\",
        r"\hline",
        r"\end{tabular}%",
    ]
    assert bold == ["", "", "max", "max", ""] and bold_by == "row"
    header, rows = parse_table(body)
    assert header[0] == r"$A\times B$" and [r[0] for r in rows] == [
        r"sep15 $\times$ jul16", r"apr16 $\times$ jul16", "Mean"]


def test_spec_layout_wins_and_validation():
    body, _, _ = render_table({**SPEC, "rules": "booktabs"}, fmt, OPTS)
    assert r"\toprule" in body and r"\hline" not in body
    with pytest.raises(TauraError, match="one direction"):
        cols = [*SPEC["columns"][:3], {"label": "J", "metric": "ticr", "bold": "min"}, SPEC["columns"][4]]
        render_table({**SPEC, "columns": cols}, fmt, OPTS)
    with pytest.raises(TauraError, match="bold_by"):
        render_table(SPEC, fmt, {**OPTS, "bold_by": "diagonal"})


def test_row_bold_mismatch(tmp_path):
    body, bold, bold_by = render_table(SPEC, fmt, OPTS)
    edited = body.replace(r"0.054 & \textbf{0.615}", r"\textbf{0.054} & 0.615")
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated/t.tex").write_text(render_header("t", "abc", "x", {}, edited) + edited)
    lock = {"artifacts": {"t": {"output": "generated/t.tex", "bold": bold, "bold_by": bold_by}}}
    [f] = check_bold(tmp_path, lock)
    assert f.id == "bold-mismatch" and "sep15" in f.location and "Unal." in f.message and "Joint" in f.message
