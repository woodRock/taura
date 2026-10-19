# Artifact scripts

Each script builds one table or figure from recorded results. taura runs it
from the code repo root with these environment variables:

- `TAURA_RESULTS`: path to JSON `{result_name: result}` for the artifact's `results`
- `TAURA_OUTPUT`: the path to write (a PDF for figures; a JSON table spec for tables)
- `TAURA_OPTIONS`: JSON of extra keys in the artifact's table in `ledger/artifacts.toml`

Never write into the paper repo. Keep output deterministic: fixed seeds, sorted
inputs, and for matplotlib `savefig(..., metadata={"CreationDate": None})`.

A table spec looks like:

```json
{"columns": [{"label": "Method"}, {"label": "F1", "metric": "f1"}],
 "rows": [["MBA", {"mean": 0.912, "std": 0.008}], ["Baseline", 0.871]]}
```

taura renders it as a booktabs tabular, formats numbers per metric, and bolds
the best cell per column when `bold` is set.
