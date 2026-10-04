# factored-hackathon-2026-bianque
The best complaint is the one that never arrives. Proactive AI customer service for LATAM banking. It scores card charges for fraud, contacts customers only when expected loss outweighs channel cost, and resolves disputes in Spanish and Portuguese with verified actions and safe human handoff.

**Live API:** [https://bianque-api-280716480355.us-central1.run.app](https://bianque-api-280716480355.us-central1.run.app) ([`/health`](https://bianque-api-280716480355.us-central1.run.app/health), [`/docs`](https://bianque-api-280716480355.us-central1.run.app/docs)). It scales to zero, so the first request after a while takes a few extra seconds.

> **Status:** the data pipeline's bronze (S3 → Parquet), silver (contracts, quarantine, dedupe, keys, PII, late arrivals) and gold (features, scores, costs, outcomes, routing, serving slice, frozen evaluation sets) layers are implemented and tested, and so is the fraud calibrator (signal gate, Bayesian blocks vs baselines, tuned model search, MLflow). So is the contact policy (versioned YAML, rule-based engine, comparison on the frozen sets). The API is deployed on Google Cloud Run with a health endpoint. Quality report and agent are not implemented yet.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- Docker (optional, for `make docker-build`)
- [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) (only to deploy, `make deploy`)

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
| `GCP_PROJECT_ID` | Google Cloud project to deploy to (`make deploy`) |
| `GCP_REGION` | Cloud Run region (default `us-central1`) |
| `GCP_SERVICE` | Cloud Run service name (default `bianque-api`) |

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
make model-search   # tuned LightGBM / logistic regression vs the calibrator (reports/model_search.md)
make gold           # rescore transaction_scores and the serving slice with the model
```

The signal gate showed that `fraud_score` already ranks at the ceiling the data allows (no legitimate transaction scores above 30.00; below it, fraud looks exactly like legitimate activity), and that behavioral features rank at chance. The learned component is therefore the map from score to probability ([ADR 0001](docs/adr/0001-fraud-signal-gate.md)):

- **Bayesian blocks:** the data places the bin edges (it finds 30.00 / 30.01 on its own), and each block has a Beta posterior, so every `p_fraud` comes with a 95% credible interval for abstention.
- **Compared on the frozen sets** with a no-skill model, the raw score, the current histogram baseline and isotonic regression, by **net benefit in USD** of the contact decisions with a paired bootstrap. Calibration is worth +$256k on test over knowing nothing (95% interval [+206k, +304k]); the three calibrators are statistically tied, and Bayesian blocks is kept for its interval.
- **A tuned search could not beat it** ([ADR 0002](docs/adr/0002-model-search.md)): LightGBM tuned with Bayesian optimization, without `fraud_score`, is no better than knowing nothing and $274k below the calibrated score on test; a hybrid that applies ML only where the score is weak is no better either.
- **The model is a committed JSON file** of aggregated counts ([`models/`](models/)); gold scores every transaction with it.

| Model (test, offline) | Log loss | Simulated net benefit (USD) |
|---|---:|---:|
| No skill (train fraud rate for everyone) | 0.00708 | 335,426 |
| Tuned LightGBM without `fraud_score` | 0.00712 | 316,952 |
| Raw score as a probability | 0.13740 | −343,192 |
| Histogram baseline | 0.00334 | 585,293 |
| Bayesian blocks | 0.00325 | 591,159 |

Design decisions, evaluation rigor and the net benefit metric: [`docs/model_design.md`](docs/model_design.md). Full results: [`reports/model_evaluation.md`](reports/model_evaluation.md), [`reports/model_search.md`](reports/model_search.md).

### Policy: when to contact, through which channel, and when a human takes over

```bash
make evaluate   # compare contact policies on the frozen sets (reports/policy_comparison.md)
```

The model only outputs a probability; [`policies/contact_policy_v1.yaml`](policies/contact_policy_v1.yaml) (versioned, labeled SYNTHETIC) decides, through [`bianque.policy.engine`](src/bianque/policy/engine.py), the same code the API uses. Every decision lists the rules that produced it, with their numbers:

- **Contact** when `p_fraud × amount > channel cost + friction`: a probability threshold per charge, (channel cost + friction) / amount, with no extra floor (none beats it on validation at any friction from USD 1.50 to 2.50). **Human review** when the model's credible interval straddles that break-even and the stake is worth an agent call. At most **one proactive alert per customer per 24 h**.
- **Channel:** Push for app users, otherwise SMS, the cheapest per message read among real-time channels (gold.channel_costs).
- **Disputes go to a human** for amounts ≥ USD 5,000 or customers with ≥ 2 complaints in a year; alerts are always automated, and "it's mine" closes without a human.
- **Actions** (dispute, provisional card block) need an authenticated session and the customer's answer; the block also needs explicit confirmation.
- **Assumptions (synthetic):** a false alert costs a legitimate customer USD 2; a contacted fraud's loss is avoided; a human case costs one agent call (USD 1.11).

| Offline simulation | Frauds caught | Legitimate customers alerted | Cases for a human | Net benefit (USD) |
|---|---:|---:|---:|---:|
| Validation, `contact_policy_v1` | 412 / 699 | 78,688 | 58 | 700,301 |
| Test, `contact_policy_v1` | 379 / 603 | 72,338 | 45 | 589,763 |
| Test, with a 0.01 floor instead (no false alerts) | 347 / 603 | 0 | 29 | 554,925 |

The guardrails (abstention, escalation, cap) cost USD 427 on test against the bare expected-value rule and keep 99.9% of cases automated. Trade-offs and alternatives: [ADR 0003](docs/adr/0003-contact-policy-v1.md).

## Deployment: Google Cloud Run

The API runs on [Cloud Run](https://bianque-api-280716480355.us-central1.run.app/health): Cloud Build builds the [`Dockerfile`](Dockerfile) remotely and the service scales to zero when idle, within the free tier.

**One-time setup:**

1. Create a Google Cloud project, link a billing account and set a budget alert (Billing → Budgets & alerts). Usage stays in the free tier.
2. Install the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install), then log in and select the project:
   ```bash
   gcloud auth login
   gcloud config set project <project-id>
   ```
3. Enable the APIs it needs:
   ```bash
   gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
   ```
4. Set `GCP_PROJECT_ID` (and optionally `GCP_REGION`, `GCP_SERVICE`) in `.env`, as in `.env.example`.

**Deploy** (after `make gold`, which builds the serving slice):

```bash
make deploy    # prints the service URL
```

- **The data stays out of git.** The serving slice (`data/gold/serving/serving.duckdb`: 300 customers, tokenized PII, no labels) is uploaded from your machine at deploy time and lives only in the private image.
- **Only what the image needs leaves the machine.** [`.gcloudignore`](.gcloudignore) is an allow-list: code, configs, policies, the model file and the slice. `.env` and the rest of the lake are never uploaded. Check with `gcloud meta list-files-for-upload`.
- **Secrets stay local.** [`scripts/deploy.sh`](scripts/deploy.sh) reads only the `GCP_*` lines of `.env`.
- **Capacity limits:** at most 2 instances × 40 concurrent requests, 1 vCPU and 512 MiB each, 60 s timeout.
- **The image has only the API's dependencies.** The `dev` and `ml` groups (MLflow, Optuna) are not installed.

## Documentation

Design decisions are documented with their evidence and the alternatives that were rejected:

| Document | Read it for |
|----------|-------------|
| [`docs/bronze_design.md`](docs/bronze_design.md) | How bronze works and why: ingestion, idempotency, layout, failure handling |
| [`docs/silver_design.md`](docs/silver_design.md) | How silver works and why: every design decision, results, tests, limitations |
| [`docs/gold_design.md`](docs/gold_design.md) | How gold works and why: point-in-time features, baseline scores, costs, slice, frozen sets |
| [`docs/model_design.md`](docs/model_design.md) | How the fraud calibrator works and why: signal gate, Bayesian blocks, selection, what the policy must know |
| [`reports/model_evaluation.md`](reports/model_evaluation.md) | Calibrators vs no skill and baselines on the frozen sets, net benefit with bootstrap intervals, results by group (`make train`) |
| [`reports/model_search.md`](reports/model_search.md) | Tuned ML (Bayesian optimization, random search) with and without `fraud_score` vs the calibrator (`make model-search`) |
| [`reports/policy_comparison.md`](reports/policy_comparison.md) | Contact policies on the frozen sets: frauds caught, false alerts, human cases, net benefit, break-even friction, results by group (`make evaluate`) |
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
| Deploy | `make deploy` | build and deploy the API to Google Cloud Run (done) |

The quality report, agent and agent evaluation are still to be written (`make pipeline` stops at the quality step until then). Pipeline outputs (`data/`) and MLflow artifacts (`mlruns/`) are git-ignored.
