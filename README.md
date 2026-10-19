# taura

A plain-text provenance ledger linking papers to code. Every number, table and
figure in a paper traces back through a checked chain:

```
paper location → artifact → result → run → commit + config + data version
```

taura is a convention, a CLI and agent rules. It's a checker, not an
experiment tracker.

## Install

```sh
pip install -e .          # or: pipx install -e .
```

Python ≥ 3.10, no runtime dependencies (`tomli` on 3.10 only).

## Pairing

`~/Desktop/<name>` (code) pairs with `~/overleaf/<name>` (paper, a git clone of
the Overleaf project). Every command works from either side. Overrides:

- `TAURA_CODE_ROOT`, `TAURA_PAPER_ROOT`: the two roots.
- `taura.toml` in the code repo: `paper = "~/overleaf/other-name"`, or
  `[papers.<name>] path = ...` for several papers (pick one with `--paper`).

## Workflow

```sh
taura init                  # ledger/, artifacts/, AGENTS.md, paper generated/
# write ledger/results/*.json and ledger/artifacts.toml
taura build                 # stage artifacts in .taura/build/ (paper untouched)
taura diff                  # value-level preview: "\res{mba/f1/mean}: 0.912 → 0.917"
taura publish               # copy to <paper>/generated/, update taura.lock, commit (never push)
taura check                 # verify the chain; exit 1 on any finding
taura status
taura adopt                 # typed numbers in the paper and the \res keys they match (report only)
```

In the paper's preamble:

```latex
\usepackage{booktabs}
\input{generated/results}   % defines \res{key}
```

Prose uses `\res{mba/f1/mean}`. An undefined key typesets a bold `??key??` and
emits a LaTeX warning. Tables are `\input{generated/table_main}`; figures are
`\includegraphics{generated/fig_alignment}`.

All commands take `--json` and `-C DIR`. Exit codes: `0` clean, `1` findings or
refused, `2` usage or config error.

## Artifacts

`ledger/artifacts.toml` defines three kinds:

- `macros`: one `\res` key per scalar in the listed results
  (`<result>/<metric>/<stat>`, or `<result>/<metric>` for bare numbers).
- `table`: a booktabs tabular with cells shown as `mean $\pm$ std`. It is
  declarative (`columns = [{metric, label, bold?}]`, one row per result) or built
  by a script that writes a JSON table spec. Set `bold = "max" | "min"` to bold
  the best displayed value per column; ties at the printed precision are all bold.
  Layout options (in the artifact's table, or in a script's spec):
  `rules = "booktabs" | "hline"`, `colspec = "@{}lrr@{}"`,
  `bold_by = "column" | "row"` (per-column `"bold": "none"` excludes a column),
  and `group_align`. Spec rows can be `{"group": "\\textit{Drifted}"}` for a
  full-width heading or `{"rule": true}` for a separator. Column labels and
  string cells are LaTeX, used verbatim; `""` makes a blank cell.
- `figure`: a PDF, either written by a script that taura runs (`script = ...`),
  or copied from a file the code repo's own pipeline already writes
  (`source = "figs/paper/fig2.pdf"`, optionally `made_by = "scripts/04_paper_figures.py"`).
  Figures are compared by **content hash**: PDF metadata that changes on every
  save (`CreationDate`, `ModDate`, `/ID`, Producer, xref offsets) is ignored, so
  re-running a figure script that draws the same thing is not a change.

Scripts run from the code repo root with `TAURA_RESULTS`, `TAURA_OUTPUT` and
`TAURA_OPTIONS` set, plus `SOURCE_DATE_EPOCH=0`. See `artifacts/README.md`
(written by `init`) and `taura.api`. The interpreter is `[build] python` in
`artifacts.toml`, else `$TAURA_PYTHON`, else `python3` on `PATH`.

Number formats go in `[format.default]`, `[format.<metric>]`,
`[format."<result>/<metric>"]`, or a glob over full keys such as
`[format."pairs/*/anchors"]` (later globs win). Options: `decimals`,
`percent` (0.912 → 91.2; the `\%` stays in the .tex), `thousands` (4396 →
`4{,}396`) and `sign` (explicit `+`).

## Numbers in prose

`taura adopt` lists every number typed into the paper body, ignoring the
preamble, comments, `\cite`/`\ref`/`\label` arguments, dimensions and column
specs. It matches each one against the code repo's results at the precision it
was typed: `0.598` matches 0.59847, `4,396` matches 4396, `81.2\%` matches
0.8121. Unique matches come with the `\res{key}` to use, and with a `[format]`
entry when needed to print the number as typed. Ambiguous ones list all
candidates. Small integers (< 100) never match, and two-decimal matches are
marked weak. It is a report only; taura never rewrites the `.tex`.

Add `% taura:ignore` to a line to suppress the heuristic checks on it.

## Checks

| id | severity | meaning |
|---|---|---|
| `hand-edit` | error | generated file's body ≠ its content-hash or lock hash; shows the diff against the last published version |
| `dangling-key` | error | `\res{key}` not in the published macros (suggests close keys, or says the key exists in code but isn't published) |
| `stale-artifact` | error | result, script, config or formatting changed since publish; or artifact unpublished or removed |
| `untracked-generated` | error | file in `generated/` not in `taura.lock` |
| `bold-mismatch` | error | bolded cell isn't the best in its column (or row, with `bold_by = "row"`) |
| `missing-figure` | error | `\includegraphics` target doesn't exist |
| `missing-source` | error | a `source` figure's file doesn't exist in the code repo |
| `missing-result` | error | an artifact lists a result with no `ledger/results/<name>.json` |
| `unused-artifact` | warn | generated file not `\input` or `\includegraphics`'d |
| `untracked-figure` | warn | `\includegraphics` of a file outside `generated/`: no provenance. If a PDF with the same content exists in the code repo, the fix names it |
| `stale-source` | warn | a `source` figure is older than its `made_by` script |
| `literal-number` | warn | a typed number matches a recorded result (3+ decimals, a percentage, or an integer ≥ 100) |
| `seed-claim` | warn | "N seeds/runs/folds" on the line of (or next to) a `\res` whose result has `n` ≠ N |

`publish` refuses if the paper has uncommitted changes, is behind its upstream
after a `git fetch` (pass `--no-fetch` to skip the fetch), has a hand-edited
generated file, or if the code tree is dirty (pass `--allow-dirty-code`). It
rewrites only artifacts whose body changed, so untouched files keep their
original provenance header.

## Decisions so far

- Tables: booktabs, `mean $\pm$ std` in one cell, no significance markers.
- Figures: PDF only.
- `publish` fetches and reports when the paper is behind; it never pulls or merges.
- Pilot: joint-align (Table II is generated).
- Still open: final name, data versioning.

## Not yet

Run provenance is phase 2: `taura run`, `taura record`, run records,
`missing-run`/`missing-commit`/`dirty-run`, `trace`, and the gpu-spooler and
alignbench integrations.

## Tests

```sh
pytest
```

The end-to-end tests build real git repos in a temp dir and cover acceptance
criteria 1–4, plus figures and prose checks. They compile with `latexmk`, or `pdflatex` as a fallback, when one
is available.
