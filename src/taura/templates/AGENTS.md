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
