# factored-hackathon-2026-bianque
The best complaint is the one that never arrives. Proactive AI customer service for LATAM banking. It scores card charges for fraud, contacts customers only when expected loss outweighs channel cost, and resolves disputes in Spanish and Portuguese with verified actions and safe human handoff.

> **Status:** the data pipeline's bronze (S3 → Parquet), silver (contracts, quarantine, dedupe, keys, PII, late arrivals) and gold (features, scores, costs, outcomes, routing, serving slice, frozen evaluation sets) layers are implemented and tested, and so is the fraud calibrator (signal gate, Bayesian blocks vs baselines, MLflow). Quality report, policy, agent and API are not implemented yet.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Docker (optional, for `make docker-build`)

## Setup

```bash
git clone <repo-url> && cd factored-hackathon-2026-bianque
make install   # uv sync --frozen (exact versions from uv.lock)
make hooks     # install pre-commit hooks (gitleaks, ruff, hygiene checks)
cp .env.example .env   # then fill in the AWS credentials and bucket name
```

Generate the PII hash key once and add it to `.env` (silver refuses to run without it):

```bash
echo "PII_HASH_KEY=$(python -c 'import secrets; print(secrets.token_hex(32))')" >> .env
```

Keep this key stable and private: changing it changes every PII token in silver (a full rebuild is needed), and anyone with the key can test guesses against the tokens.

Run `make` or `make help` to list all commands.

### Environment variables

| Variable | Description |
|----------|-------------|
| `AWS__ACCESS_KEY_ID` | Access key for the source bucket (read-only) |
| `AWS__SECRET_ACCESS_KEY` | Secret key for the source bucket |
| `AWS__REGION` | Bucket region (default `us-east-2`) |
| `AWS__BUCKET_NAME` | Source bucket holding the CSVs under `data/` |
| `LAKE_ROOT` | Local lake directory (default `data`, git-ignored) |
| `PII_HASH_KEY` | Secret key for PII tokens in silver; keep it stable (changing it changes every token) |

If the key variables are empty, boto3 falls back to its default credential chain (`~/.aws`, SSO, instance role).

## Data pipeline

### Bronze: S3 CSV → Parquet

```bash
make bronze-plan   # list pending files and MB per dataset, downloads nothing
make bronze        # ingest pending files (progress bar in bytes)
```

- **No raw copy:** each CSV is streamed from S3 straight into Parquet, so the data is not stored twice.
- **Faithful copy:** every column is kept as text and only empty cells become null; types are fixed in silver.
- **Lineage columns:** `_source_key`, `_source_etag`, `_ingested_at` on every row.
- **Incremental and idempotent:** `data/_meta/manifest.parquet` records key + ETag of every ingested object. Re-runs only process new or modified objects; failed files are retried on the next run, and progress is kept even if interrupted with Ctrl+C.
- **Atomic writes:** files are written to `*.tmp` and renamed, so a Parquet file is never half-written.

Design decisions and their rationale: [`docs/bronze_design.md`](docs/bronze_design.md).

Source layout in the bucket (7,671 CSVs, ~5.1 GB):

| Kind | S3 key | Datasets |
|------|--------|----------|
| Facts (daily partitions) | `data/<dataset>/year=YYYY/month=MM/day=DD/<file>.csv` | `transactions`, `digital_events`, `campaign_sends`, `call_center_interactions`, `call_transcripts`, `complaints`, `satisfaction_surveys` |
| Dimensions and references | `data/<dataset>.csv` | `customers`, `products`, `branches`, `service_agents`, `marketing_campaigns`, `daily_exchange_rates` |

Bronze mirrors that layout:

```
data/
├── _meta/manifest.parquet
└── bronze/
    ├── transactions/year=2026/month=06/day=17/<file>.parquet
    └── customers/customers.parquet
```

### Silver: typed, validated, private

```bash
make silver                                         # all tables; facts incrementally
uv run python -m bianque.pipeline.silver --full     # ignore the watermark, rebuild all
```

Driven by one schema contract per table in [`contracts/`](contracts/). For each table, silver:

- **Enforces the schema:** a missing column fails the build; new columns pass as text.
- **Types and normalizes** (`México → Mexico`, list columns, integers sent as floats) and **quarantines** rows that do not fit, with a reason, instead of dropping them.
- **Deduplicates** by primary key with a deterministic tie-break.
- **Checks foreign keys** against the parent silver tables: orphans go to quarantine, except two keys that are random IDs in the source and are set to NULL.
- **Fills `amount_usd`** with the source's booking rate (350 ARS, 4,000 COP per USD).
- **Tokenizes PII** with HMAC-SHA256 and replaces birth dates by age bands.
- **Handles late arrivals** with a watermark: facts rebuild only the months touched by new bronze files plus a 7-day trailing window (40 s vs 3 min for a full build).

```
data/silver/
├── customers/data.parquet                        # dimensions
├── transactions/process_month=2024-01/*.parquet  # facts, one folder per month
└── _quarantine/<table>/                          # rejected rows with _reason
```

Results on the real data: 23.5M rows, 0 quarantined, 0 duplicates, 150,826 orphaned branch keys nullified.

Design decisions and their rationale: [`docs/silver_design.md`](docs/silver_design.md).

### Gold: what the proactive loop needs

```bash
make gold                                            # rebuild gold, verify frozen eval sets
uv run python -m bianque.pipeline.gold --refreeze    # accept an intended change to eval sets
```

Bianque contacts a customer only when `p_fraud × amount_usd > channel cost + friction for a legitimate customer`. Gold provides every term of that rule and what the agent needs afterwards:

| Output | Used for |
|--------|----------|
| `transaction_features` | Point-in-time fraud features: only information strictly before each transaction |
| `transaction_scores` | What the proactive scan reads: calibrated `p_fraud`, model version, timestamp (baseline: the bank's `fraud_score`, calibrated on train) |
| `channel_costs` | Cost, delivery and response per channel |
| `customer_360` | Agent context, fairness breakdowns, the app (snapshot, no labels) |
| `agent_routing` | Handoff by language and specialty, with measured performance |
| `dispute_outcomes` | What a complaint costs when it does arrive (ROI value side) |
| `service_cost_baseline` | Status-quo service cost (ROI baseline) |
| `serving/serving.duckdb` | 300-customer slice the deployed API reads, without labels |

Frozen out-of-time evaluation sets live in [`eval/`](eval/README.md), with their SHA-256 in a committed manifest; every gold run verifies them. Costs that the data does not contain are **synthetic, versioned assumptions** in [`policies/`](policies/README.md).

Key finding: the only fraud signal in this dataset is the bank's own `fraud_score`; behavioral features show no difference between fraud and non-fraud, so the calibrated baseline is the bar any model has to clear. A signal gate confirmed it before training (a model on behavior ranks at chance), so the learned component is a calibrator of that score ([ADR 0001](docs/adr/0001-fraud-signal-gate.md)).

Design decisions and their rationale: [`docs/gold_design.md`](docs/gold_design.md).

### Model: a calibrated fraud probability

```bash
make label-signal   # signal gate: what the data can teach (reports/label_signal.md)
make train          # fit and compare calibrators on the frozen sets, log to MLflow
make gold           # rescore transaction_scores and the serving slice with the model
```

The signal gate showed that `fraud_score` already ranks at the ceiling the data allows (no legitimate transaction scores above 30.00; below it, fraud looks exactly like legitimate activity), and that behavioral features rank at chance. The learned component is therefore the map from score to probability ([ADR 0001](docs/adr/0001-fraud-signal-gate.md)):

- **Bayesian blocks:** the data places the bin edges (it finds 30.00 / 30.01 on its own), and each block has a Beta posterior, so every `p_fraud` comes with a 95% credible interval for abstention.
- **Compared on the frozen sets** with the raw score, the current histogram baseline and isotonic regression. It has the best log loss and the best simulated net benefit on validation (selection) and on test (report).
- **The model is a committed JSON file** of aggregated counts ([`models/`](models/)); gold scores every transaction with it.

| Calibrator (test, offline) | Log loss | Simulated net benefit (USD) |
|---|---:|---:|
| Raw score as a probability | 0.13740 | −343,192 |
| Histogram baseline | 0.00334 | 585,293 |
| Bayesian blocks | 0.00325 | 591,159 |

Design decisions and their rationale: [`docs/model_design.md`](docs/model_design.md). Full results: [`reports/model_evaluation.md`](reports/model_evaluation.md).

## Documentation

Design decisions are documented with their evidence and the alternatives that were rejected:

| Document | Read it for |
|----------|-------------|
| [`docs/bronze_design.md`](docs/bronze_design.md) | How bronze works and why: ingestion, idempotency, layout, failure handling |
| [`docs/silver_design.md`](docs/silver_design.md) | How silver works and why: every design decision, results, tests, limitations |
| [`docs/gold_design.md`](docs/gold_design.md) | How gold works and why: point-in-time features, baseline scores, costs, slice, frozen sets |
| [`docs/model_design.md`](docs/model_design.md) | How the fraud calibrator works and why: signal gate, Bayesian blocks, selection, what the policy must know |
| [`reports/model_evaluation.md`](reports/model_evaluation.md) | Calibrators vs baselines on the frozen sets, simulated contact outcomes, results by group (`make train`) |
| [`docs/silver_data_findings.md`](docs/silver_data_findings.md) | What the source data really looks like vs the data dictionary (keys, NULLs, process dates, cross-table links, the fraud signal) |
| [`reports/label_signal.md`](reports/label_signal.md) | The signal gate run before training: what is learnable in the data, with denominators (`make label-signal`) |
| [`docs/adr/`](docs/adr/) | Architecture decision records: deviations from the build plan, with evidence |
| [`contracts/README.md`](contracts/README.md) | The schema contract format |
| [`fixtures/README.md`](fixtures/README.md) | Team-generated synthetic test data and what it covers |
| [`policies/README.md`](policies/README.md) | Synthetic, versioned assumptions (costs) |
| [`eval/README.md`](eval/README.md) | Frozen evaluation sets and leakage controls |

## Development

```bash
make format    # auto-format and fix with ruff
make lint      # check formatting and lint without modifying files
make test      # run pytest (tests/)
```

Pre-commit hooks run gitleaks (secret scanning), basic file checks and ruff on every commit. Never commit credentials; keep them in a local `.env` (ignored by git).

## Workflow

| Step | Command | What it does |
|------|---------|--------------|
| Data | `make pipeline` | bronze (done) → silver (done) → gold (done) → quality report |
| Model | `make train` | fit the fraud calibrator, compare with baselines, log to MLflow (done) |
| Eval | `make evaluate` | compare contact policies and run agent evaluation |
| Serve | `make serve` | run the FastAPI app locally |

The quality report, evaluation and serving modules are still to be written (`make pipeline` stops at the quality step until then). Pipeline outputs (`data/`) and MLflow artifacts (`mlruns/`) are git-ignored.
