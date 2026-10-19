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
pip install -e '.[tui]'   # with the TUI (Textual)
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
taura run -- python3 train.py --config c.yaml --seed 3   # run and record an experiment
taura runs                  # the run records
taura trace TARGET          # walk a claim back: a \res key, main.tex:142, or an artifact
taura papers                # every paired code and paper repo under the roots
taura tui                   # browse papers, claims and their provenance (read-only)
```

## Recording runs

Every experiment run gets a record in `ledger/runs/<id>.toml`:

- the commit, and whether the tree was dirty;
- the command, its `--config` file (hashed), its `--seed`, and the scripts it named;
- the host, and the start and finish times;
- the exit code;
- the datasets;
- the outputs it wrote, with content hashes.

- **On your machine:** `taura run -- <cmd>`. Output and exit code pass through
  unchanged. The record is written at start and again at the end. The command
  sees `TAURA_RUN_ID`.
- **Through gpu-spooler:** nothing to add. With taura installed and a `ledger/`
  in the repo, the daemon records each task: the commit the server actually ran
  at start, and the exit code at the end. `task fetch PATHS --after ID` records
  the files it brought back. A `task sync` of a dirty tree is recorded as dirty,
  with the commit it was made on.
- **Anything else:** `taura record ID --event start|finish|fetch ...`.

Datasets are declared in `taura.toml`, each with a human label and a content
hash. taura caches file hashes by size and mtime in `.taura/`, so only changed
files are re-read:

```toml
[data.fish-oil]
path = "data/fish_oil"
label = "v1"

[runs]
watch = ["src/"]          # code whose change makes earlier results suspect (besides the run's own scripts)
```

Results link to runs on their own: a result whose source file has the same
content hash as a run's output came from that run. A result can also list run
IDs in `runs`. Figures link the same way, so the exporter doesn't need to change.
`taura trace` and the TUI's Runs tab show the links.

## Tracing claims

`taura trace` walks from the paper to what produced it. A target is a `\res` key
(`mba/f1/mean`), a place in the paper (`main.tex:142`, everything on that line),
or an artifact name. For each claim it shows:
- what the paper prints and what the code's results give now;
- the result file and its hash, and whether it changed since the last publish;
- the source the result was exported from, with its hash and the commits and
  environment it was computed at;
- the run records (phase 2);
- the artifact that publishes it, and the publish commit.

`--json` gives the same as data.

`taura tui` shows this as a browser. The papers screen lists every pair. Each
paper has tabs for Claims, Artifacts, Typed numbers, Runs and Findings, with the trace
of the highlighted row on the right. Keys: `enter` open, `e` open the line in
`$EDITOR`, `/` filter, `r` reload, `escape` back, `q` quit. Started inside a repo,
it opens that paper.

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

### Deciding what each number is, with an agent

Matching by value can't settle a tie: `0.598` may be both the Joint model's
drifted-pairs mean and one pair's score. It can't tell a result from a
coincidence either, such as a Spearman ρ of 0.40 that equals a TIC r to two
decimals, or a typed result the ledger doesn't have yet. Reading the sentence
does settle it, so taura hands that judgement to an agent and keeps the
bookkeeping itself:

```sh
taura adopt --todo --json      # numbers needing a decision, each with an id, sentence,
                               # paragraph, section and candidate keys
taura resolve ID KEY --reason "the sentence is about aug2015 × jul2016"
taura resolve ID --none --reason "a postcode"            # not a result: stop warning
taura resolve ID --unrecorded --reason "anchor count"    # a result the ledger lacks
taura resolve --accept-unique                            # every single strong match
taura adopt --agent "claude -p"                          # or let an agent CLI decide
```

Any agent can drive `adopt --todo --json` and `resolve` itself; the shipped
`AGENTS.md` explains how. `--agent CMD` sends the undecided numbers in batches
(`--batch`, `--limit`) to any command that reads a prompt on stdin and prints
JSON. Replies are validated like `resolve`: a "result" must name one of that
number's candidates. Decisions go to `ledger/adopt.toml` in the code repo,
keyed by a literal id that survives line moves and neighbouring rewrites. They
are never asked again.

taura still never edits the `.tex`. The agent replaces each number decided as a
result with `\res{key}`, and `taura check` then verifies the rewrite:
`adopt-mismatch` fires if the key prints something other than what the paper
said, e.g. `12.2` where the text had `12.25`.

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
| `literal-number` | warn | a typed number matches a recorded result (3+ decimals, a percentage, or an integer ≥ 100), or was decided to be one and isn't rewritten yet |
| `unrecorded-result` | warn | a typed number decided to be a computed result the ledger doesn't have |
| `adopt-mismatch` | warn | a number rewritten as `\res{key}` now prints differently from what the paper said |
| `missing-run` | error | a result names a run with no record |
| `missing-commit` | error | a run's commit is not in the code repo |
| `dirty-run` | warn | a published result or figure comes from a run with uncommitted changes |
| `failed-run` | warn | ... from a run that exited non-zero |
| `code-changed-since-run` | warn | ... from a run whose scripts, config or `[runs] watch` paths changed since its commit |
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

- The alignbench adapter.
- A "changed since publish" view in the TUI.
- TUI actions (check, diff, publish, resolve).

## Tests

```sh
pytest
```

The end-to-end tests build real git repos in a temp dir and cover acceptance
criteria 1–4, plus figures and prose checks. They compile with `latexmk`, or `pdflatex` as a fallback, when one
is available.
