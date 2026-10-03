# factored-hackathon-2026-bianque
The best complaint is the one that never arrives. Proactive AI customer service for LATAM banking. It scores card charges for fraud, contacts customers only when expected loss outweighs channel cost, and resolves disputes in Spanish and Portuguese with verified actions and safe human handoff.

> **Status:** the data pipeline's bronze (S3 → Parquet) and silver (contracts, quarantine, dedupe, keys, PII, late arrivals) layers are implemented and tested. Gold, quality report, model and API are not implemented yet.

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

## Documentation

Design decisions are documented with their evidence and the alternatives that were rejected:

| Document | Read it for |
|----------|-------------|
| [`docs/bronze_design.md`](docs/bronze_design.md) | How bronze works and why: ingestion, idempotency, layout, failure handling |
| [`docs/silver_design.md`](docs/silver_design.md) | How silver works and why: every design decision, results, tests, limitations |
| [`docs/silver_data_findings.md`](docs/silver_data_findings.md) | What the source data really looks like vs the data dictionary (keys, NULLs, process dates, quirks) |
| [`contracts/README.md`](contracts/README.md) | The schema contract format |
| [`fixtures/README.md`](fixtures/README.md) | Team-generated synthetic test data and what it covers |

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
| Data | `make pipeline` | bronze (done) → silver (done) → gold → quality report |
| Model | `make train` | train fraud model and log to MLflow |
| Eval | `make evaluate` | compare contact policies and run agent evaluation |
| Serve | `make serve` | run the FastAPI app locally |

Gold, quality, model, evaluation and serving modules are still to be written. Pipeline outputs (`data/`) and MLflow artifacts (`mlruns/`) are git-ignored.
