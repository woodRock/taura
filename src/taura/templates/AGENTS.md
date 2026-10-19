# Rules for agents in this repo (taura provenance)

- Never type a numeric result into any .tex file. Add it to a result in
  ledger/results/ and reference it with \res{key}.
- Never edit anything in the paper's generated/ folder. Regenerate it.
- Never copy a figure into the paper by hand. Add a figure artifact in
  ledger/artifacts.toml and publish it.
- Launch experiments with `taura run -- <cmd>` (or via gpu-spooler with
  taura recording enabled) so every run gets a record.
- Before saying a task is done, run `taura check`. If it reports
  anything, report the findings to the user. Do not "fix" a hand-edit
  finding by overwriting the file; a collaborator may have made it.
- Use `taura diff` to describe what a change does to the paper.
- Never `git push` the paper repo.

## Moving typed numbers into \res (taura adopt)

1. `taura adopt --todo --json` lists the typed numbers that need a decision,
   each with an `id`, the sentence and paragraph around it, and the
   candidate result keys.
2. Read the sentence. Decide what the number is and record it:
   - `taura resolve ID KEY --reason "..."` if it reports that result;
   - `taura resolve ID --none --reason "..."` if it isn't a result (year,
     setting, sample size, a value from another paper, a coincidence);
   - `taura resolve ID --unrecorded --reason "..."` if it is a computed
     result but no candidate fits (report these: the ledger needs them).
   `taura resolve --accept-unique` records every number with exactly one
   strong match. Or let an agent CLI decide: `taura adopt --agent "claude -p"`.
3. Replace each number decided as a result with `\res{KEY}` in the .tex.
   Change nothing else on the line.
4. `taura check`: `adopt-mismatch` means a key doesn't print what the paper
   said (add a [format] entry). Report anything else to the user.
