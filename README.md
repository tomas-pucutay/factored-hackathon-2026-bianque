# factored-hackathon-2026-bianque
The best complaint is the one that never arrives. Proactive AI customer service for LATAM banking. It scores card charges for fraud, contacts customers only when expected loss outweighs channel cost, and resolves disputes in Spanish and Portuguese with verified actions and safe human handoff.

> **Status:** early scaffold. Bronze ingestion (S3 → Parquet) is implemented; silver, gold, model and API modules are not implemented yet.

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

Run `make` or `make help` to list all commands.

### Environment variables

| Variable | Description |
|----------|-------------|
| `AWS__ACCESS_KEY_ID` | Access key for the source bucket (read-only) |
| `AWS__SECRET_ACCESS_KEY` | Secret key for the source bucket |
| `AWS__REGION` | Bucket region (default `us-east-2`) |
| `AWS__BUCKET_NAME` | Source bucket holding the CSVs under `data/` |
| `LAKE_ROOT` | Local lake directory (default `data`, git-ignored) |

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

## Development

```bash
make format    # auto-format and fix with ruff
make lint      # check formatting and lint without modifying files
make test      # run pytest (tests/)
```

Pre-commit hooks run gitleaks (secret scanning), basic file checks and ruff on every commit. Never commit credentials; keep them in a local `.env` (ignored by git).

## Planned workflow

These targets are defined in the `Makefile` but their modules are still to be written:

| Step | Command | What it does |
|------|---------|--------------|
| Data | `make pipeline` | bronze (done) → silver (contracts, quarantine) → gold → quality report |
| Model | `make train` | train fraud model and log to MLflow |
| Eval | `make evaluate` | compare contact policies and run agent evaluation |
| Serve | `make serve` | run the FastAPI app locally |

Pipeline outputs (`data/`) and MLflow artifacts (`mlruns/`) are git-ignored.
