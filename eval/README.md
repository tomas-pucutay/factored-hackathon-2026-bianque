# Evaluation

Evaluation artifacts live here, apart from the data lake, so the leakage controls are visible.

## Frozen sets (`frozen/`)

Out-of-time slices of `gold.transaction_features`, split on `process_date`:

| Set | process_date | Rows | Fraud |
|-----|--------------|-----:|------:|
| train (not frozen; read from gold) | before 2025-07-01 | | |
| `fraud_validation` | 2025-07-01 to 2025-12-31 | 743,934 | 699 |
| `fraud_test` | 2026-01-01 to 2026-06-17 | 685,522 | 603 |

- `frozen/manifest.json` is committed: SHA-256, row and fraud counts and split dates of each
  file. The Parquet files are git-ignored (size, and the Datathon data is not republished).
- Every `make gold` rebuilds the sets and compares them with the manifest. A different hash
  fails the run and keeps the frozen files; an intended change needs
  `uv run python -m bianque.pipeline.gold --refreeze` and a commit of the new manifest.
- A fresh clone rebuilds the files from the data and must reproduce the committed hashes
  (they depend on the DuckDB version pinned in `uv.lock`).
- The sets contain features and the label, never model scores: they do not depend on any
  model version. Models train and calibrate on train only.

Design rationale: [`docs/gold_design.md`](../docs/gold_design.md) §4.10.
