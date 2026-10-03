# Gold design

How the gold layer works and **why** each decision was made. Previous layers:
[`bronze_design.md`](bronze_design.md), [`silver_design.md`](silver_design.md). The data
findings behind several decisions are in [`silver_data_findings.md`](silver_data_findings.md)
§9.

Code: [`sql/gold/`](../sql/gold/), [`src/bianque/pipeline/gold.py`](../src/bianque/pipeline/gold.py),
[`serving.py`](../src/bianque/pipeline/serving.py),
[`models/baselines.py`](../src/bianque/models/baselines.py),
[`evaluation/frozen_sets.py`](../src/bianque/evaluation/frozen_sets.py),
[`policies/`](../policies/).

## Contents

1. [Running it](#1-running-it)
2. [What gold contains](#2-what-gold-contains)
3. [Layout](#3-layout)
4. [Design decisions](#4-design-decisions)
5. [What the data says](#5-what-the-data-says)
6. [Results on the real data](#6-results-on-the-real-data)
7. [Tests](#7-tests)
8. [Limitations and open points](#8-limitations-and-open-points)

## 1. Running it

```bash
make gold                                            # build gold, verify the frozen eval sets
uv run python -m bianque.pipeline.gold --refreeze    # accept changed eval sets
```

Requires silver (`make silver`). Gold is rebuilt in full from silver on every run.

## 2. What gold contains

Bianque contacts customers **before** they complain. Each gold table serves one part of that
loop:

| Output | Grain | Used for |
|--------|-------|----------|
| `transaction_features` | 1 row per transaction (4.4M) | Fraud model features, point-in-time |
| `transaction_scores` | 1 row per transaction | What the proactive scan reads: calibrated `p_fraud`, model version, timestamp |
| `score_calibration` | 1 row per score bin | Audit trail behind every `p_fraud` |
| `channel_costs` | 1 row per channel | Cost side of the expected-value rule |
| `customer_360` | 1 row per customer | Agent context, fairness breakdowns, the app |
| `agent_routing` | 1 row per agent | Handoff by language and specialty |
| `dispute_outcomes` | 1 row per complaint | What a complaint costs when it does arrive (value side of ROI) |
| `service_cost_baseline` | 1 row per contact reason × interaction type | Status-quo cost (ROI baseline) |
| Serving slice | DuckDB file, ~300 customers | What the deployed API reads |
| Frozen evaluation sets | `eval/frozen/`, outside gold | Out-of-time validation and test, hashed |

The expected-value rule these tables feed:

```
contact the customer  if  p_fraud × amount_usd  >  channel cost  +  friction for a legitimate customer
                          transaction_scores      channel_costs     policies/cost_assumptions_v1.yaml
```

## 3. Layout

```
data/gold/
├── transaction_features/process_month=YYYY-MM/*.parquet
├── transaction_scores/process_month=YYYY-MM/*.parquet
├── score_calibration/  channel_costs/  customer_360/  agent_routing/
├── dispute_outcomes/   service_cost_baseline/                 # one file each
└── serving/serving.duckdb                                     # the slice for the app
eval/frozen/
├── manifest.json            # committed: hashes, rows, split dates
├── fraud_validation.parquet # git-ignored
└── fraud_test.parquet       # git-ignored
policies/cost_assumptions_v1.yaml   # SYNTHETIC, committed
```

## 4. Design decisions

### 4.1 Scope follows the proactive design

**Decision:** the first gold list (written for a reactive dispute assistant) was revised with
the team for Bianque's proactive flow:

- `dispute_cases` became `dispute_outcomes`: not the workload of a dispute desk, but what a
  complaint costs when it arrives (compensation, SLA, regulator, resolution time).
- Added `transaction_scores`, `channel_costs`, `service_cost_baseline` and `agent_routing`:
  without them the decision to contact has no probability, no cost side, no baseline and no
  handoff.
- Frozen evaluation sets moved out of gold to `eval/`: they are evaluation artifacts, and
  keeping them apart makes the leakage controls visible.

### 4.2 Portable SQL per table; Python only where SQL is not enough

**Decision:** each gold table is one SQL file in `sql/gold/` that reads silver tables by name
(and gold tables built before it). Python handles the three steps that need more than a
query: the calibrated scores (fit then apply), the serving slice (a separate database file) and
the frozen sets (hashing and verification).

**Why:** the same as silver: SQL that runs on DuckDB today and can move to Athena or Spark.

### 4.3 Point-in-time features

**Decision:** every feature of a transaction at time `t` uses only information strictly before
`t`:

- Windows end at `1 microsecond preceding`: a transaction never sees itself or another
  transaction at the same instant.
- Complaints, interactions, logins and digital errors are counted with `ASOF` joins on
  `event time < t`, using cumulative counts (`events in (t − w, t) = cum(< t) − cum(≤ t − w)`).
- Windows: 24 h, 7 d, 30 d for transactions; 90 d complaints; 30 d interactions; 24 h logins.
  Customers transact rarely (median 29 transactions in 3 years, ~18 days apart), so 1-hour
  windows would be almost always empty.

Deliberately **not** used:

| Not used | Why |
|----------|-----|
| `is_fraud` of earlier transactions | Labels are known only after investigation |
| Snapshot fields (balance, status, credit_score, `last_*`) | They reflect the export date, not time `t` |
| `digital_events.product_id`, `complaints.affected_product_id` | They point to other customers' products |
| `registration_date`, `opening_date` | Random with respect to activity (36% of customers transact before registering); replaced by observed tenure `days_since_first_tx` |

**Proof:** a test adds a future transaction and checks that every feature of the earlier
transactions is unchanged.

### 4.4 customer_360 is a snapshot without labels

**Decision:** one row per customer as of the last process date (2026-06-17), with no fraud
label or anything derived from it.

**Why:** it is not point-in-time, so it must not feed a model; leaving labels out removes the
temptation. Model features come from `transaction_features`.

### 4.5 transaction_scores: a calibrated baseline now, the model later

**Decision:** until the model exists, `transaction_scores` holds the bank's own `fraud_score`
calibrated to a probability, `model_version = baseline_fraud_score_v1`. The trained model will
write its own version into the same table.

- Histogram calibration: `fraud_score` in bins of 5 points; each bin's probability is its
  fraud rate in the **train split only** (process_date before 2025-07-01), smoothed with one
  pseudo-transaction at the base rate. Unscored transactions (20%) get the unscored train rate.
- Every bin of the grid is present; a bin with no train data takes the nearest observed bin
  (`is_filled`), never the base rate, which would make an unseen high score look safe.
- `score_calibration` keeps the bins behind every probability (audit trail);
  `is_out_of_sample` marks transactions after the train split.

**Why:** the scan, the slice and the audit trail can be built and tested today; the model is
then a drop-in replacement. And because `fraud_score` is the only signal in the data (§5),
this baseline is the bar the model has to clear.

**Evidence that changed it:** a first prior weight of 10 predicted 0.92 for bins where 100% of
train transactions were fraud; a weight of 1 predicts 0.99. Out of sample:

| Score range | Predicted | Observed (validation + test) |
|-------------|----------:|-----------------------------:|
| < 30 | 0.0003 | 0.0003 |
| 30–35 | 0.238 | 0.188 (239 rows) |
| ≥ 35 | 0.991 | 1.000 |
| no score | 0.0011 | 0.0009 |

### 4.6 channel_costs: unknown is not zero

**Decision:** cost and response by channel from `campaign_sends`. Delivery rate over sends;
open, click and conversion rates over delivered sends. Voice and WhatsApp have no response
tracking (opens NULL, clicks and conversions always false over 400k sends), so their rates
are NULL and `opens_tracked` is false.

**Why:** reporting 0% response would make Voice and WhatsApp look useless in the
expected-value rule, when their response is simply unknown. `send_cost` has no currency in the
dictionary but is identical across the three countries (e.g. SMS 0.10, Voice 0.20), so it is
treated as USD.

### 4.7 service_cost_baseline: measured volumes, assumed costs

**Decision:** volumes, handling times, first-contact resolution and escalation are measured;
cost per contact comes from `policies/cost_assumptions_v1.yaml`, labeled **SYNTHETIC**. Calls
and video cost measured minutes × cost per minute; chat and email (no duration in the data) a
flat cost per contact. Every row records the assumptions version.

**Why:** the data has no cost per contact. Making the assumption a versioned, reviewable file
(instead of a number inside SQL) keeps the ROI honest: changing it is a new version in git.

### 4.8 dispute_outcomes: one row per complaint, amounts treated as USD

**Decision:** every complaint with its outcome (category, regulator channel, SLA breach,
resolution time, compensation), plus segment, country and age band for fairness. Charge
disputes are categories Transactions and Fees.

- Amounts are **not** converted by the complaint's `currency`: the label is random (claimed
  amounts are uniform 0–5,000 with the same distribution under ARS, COP, MXN and USD).
  Converting would distort amounts up to 4,000×. Amounts are treated as USD
  (`*_usd_assumed`) and the label is kept.
- `affected_product_id` is never joined (it belongs to another customer in 100% of cases);
  `affected_product_is_customers` records the check.

**Why:** per-complaint grain lets the ROI analysis aggregate any way it needs. The source does
not link complaints to transactions (§5), and Bianque does not need that link: it needs the
cost of a complaint that was not prevented.

### 4.9 agent_routing: measured performance, explicit flags

**Decision:** languages as ISO lists (`speaks_es`, `speaks_pt`, `speaks_en`), specialty flags,
availability, and performance measured over the last 90 days (volume, first-contact
resolution, escalation, CSAT from surveys) next to the profile's own `avg_csat`. An agent
without a specialty is not a specialist (`false`, not NULL). `assigned_branch_id` is not used
(random ID).

### 4.10 Frozen evaluation sets: fixed, hashed, verified

**Decision:**

- Out-of-time split on `process_date`: train < 2025-07-01 ≤ validation < 2026-01-01 ≤ test <
  2026-06-18. The end of test is fixed so new data never changes the test set.
- Validation and test are frozen; train is read from gold by models.
- No model scores inside: the sets do not depend on any model version.
- Each file is written deterministically (sorted by `transaction_id`, one thread, fixed row
  groups); its SHA-256, rows and fraud count are recorded in `eval/frozen/manifest.json`
  (committed; the Parquet files are git-ignored).
- Every gold run rebuilds the sets and compares: same hash, nothing changes; a different hash
  fails the run and keeps the frozen files, unless `--refreeze`. A fresh clone rebuilds the
  files and must reproduce the committed hashes.

**Why:** evaluation numbers are only comparable if they come from the same rows. A silent
change in the test set (late data, a bug fix upstream) would make an old and a new model
incomparable without anyone noticing.

### 4.11 Serving slice: small, deterministic, no labels

**Decision:** one DuckDB file with ~300 customers: proactive targets (recent transaction with
`p_fraud ≥ 0.5`), customers with a recent charge dispute, and a sample, each group at most a
third, ordered by `md5(customer_id)`. It holds their 360, products, last 90 days of
transactions with scores, and disputes, plus all agents, channel costs and cost assumptions.
The build fails if any table carries `is_fraud`.

**Why:** the deployed API cannot reach the lake. A deterministic selection gives the same demo
for the same data. Labels in the slice would let the app "decide" with the answer.

### 4.12 Full rebuild from silver

**Decision:** gold is rebuilt in full on every run (no watermark).

**Why:** a late transaction changes the history of every later transaction of that customer,
so incremental point-in-time features would need careful invalidation. A full rebuild takes a
few minutes and is trivially correct; the late-arrival fixture test proves gold recomputes the
history.

## 5. What the data says

Findings measured while building gold (details in `silver_data_findings.md` §9) that matter
for the rest of the project:

- **The only fraud signal is `fraud_score`.** Channel, country, hour, type and amount relative
  to the customer's history show no difference between fraud and non-fraud. Point-in-time
  features confirm it (e.g. 0.036 vs 0.040 transactions in the previous 24 h). A model on
  behavioral features alone should not be expected to beat the calibrated baseline; the frozen
  sets make that comparison possible.
- **Complaints are not caused by fraud** (customers with fraud complain within 30 days at the
  same ~1% rate as anyone) and are not linked to transactions.
- **Status-quo service cost** (assumptions v1): ~19k contacts and ~USD 29.5k per month.
- **Complaint cost when it arrives:** ~20% breach SLA, ~1% reach the regulator, median 15–16
  days to resolve, ~7% get compensation (median ~USD 250 assumed).
- **Handoff capacity:** 1,090 agents available, 115 speak Portuguese, but only 7 are available
  fraud specialists who speak Portuguese.

## 6. Results on the real data

Full build on 2026-10-04 (silver as of 2026-06-17):

| Output | Rows | Size |
|--------|-----:|-----:|
| `transaction_features` | 4,425,008 | 294 MB |
| `transaction_scores` | 4,425,008 | 136 MB |
| `customer_360` | 150,000 | 8.1 MB |
| `dispute_outcomes` | 67,095 | 2.4 MB |
| `agent_routing` | 1,200 | 40 KB |
| `service_cost_baseline` | 30 | 12 KB |
| `channel_costs` | 5 | 8 KB |
| `score_calibration` | 21 | 8 KB |
| Serving slice | 300 customers: 58 proactive targets, 100 recent disputes, 142 sampled; 904 products, 952 transactions (59 with `p_fraud ≥ 0.5`), 227 disputes, 1,200 agents | 3.1 MB |

| Frozen set | Rows | Fraud | SHA-256 (first 12) |
|------------|-----:|------:|--------------------|
| `fraud_validation` | 743,934 | 699 | `075d4b11c88a` |
| `fraud_test` | 685,522 | 603 | `2590b99d7c5b` |

A full gold run takes about 6 minutes (peak 3.4 GB); `transaction_features` is about 4 of
them. Two consecutive full runs reproduced the same hashes, and the second left the manifest
untouched.

## 7. Tests

| File | Covers |
|------|--------|
| `tests/test_gold.py` | Runner; point-in-time features (same-instant ties, windows, ASOF counts) and invariance to future transactions; customer_360 snapshot without labels; channel cost denominators and unknown response; service cost pricing; dispute outcomes amounts and product check; agent flags and 90-day performance; cost assumptions loading; calibration on train only and nearest-bin filling |
| `tests/test_serving.py` | Slice selection by reason, last 90 days only, no labels, slice info, determinism |
| `tests/test_frozen_sets.py` | First freeze, identical rerun, fresh clone reproduces hashes, changed data fails and keeps files, refreeze |
| `tests/test_late_arrival.py` | Team fixture: gold recomputes history after a late partition and sees the corrected duplicate |

## 8. Limitations and open points

- **Complaint amounts are treated as USD** because their currency label is random; this sets
  the value side of the ROI and should be confirmed by the team.
- **Costs are synthetic** (`policies/cost_assumptions_v1.yaml`): service cost per contact and
  the friction cost are assumptions, not measurements.
- **Point-in-time uses event time, not availability time.** A transaction that arrived late
  in reality would not have been visible when later transactions were scored; the data has no
  real late arrivals, so this is documented, not modeled.
- **Voice and WhatsApp response is unknown;** the contact policy needs an assumption for them.
- **The serving slice is git-ignored** (`*.duckdb`) like all data; how it reaches the deployed
  app (image build, artifact store) is decided with the deployment.
- **`transaction_scores` is a baseline** until the model step writes a trained version.
