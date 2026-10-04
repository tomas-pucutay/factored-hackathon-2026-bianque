# Model design

How the learned component works and **why**. It sits between gold and the policy: it turns
the bank's `fraud_score` into a calibrated probability with an uncertainty interval, and it
never decides anything. Previous layer: [`gold_design.md`](gold_design.md).

Code: [`models/calibration.py`](../src/bianque/models/calibration.py),
[`models/train.py`](../src/bianque/models/train.py),
[`models/scoring.py`](../src/bianque/models/scoring.py),
[`evaluation/label_signal.py`](../src/bianque/evaluation/label_signal.py),
[`evaluation/model_search.py`](../src/bianque/evaluation/model_search.py). Model file:
[`models/fraud_calibrator_bayes_blocks_v1.json`](../models/fraud_calibrator_bayes_blocks_v1.json).
Results: [`reports/label_signal.md`](../reports/label_signal.md),
[`reports/model_search.md`](../reports/model_search.md),
[`reports/model_evaluation.md`](../reports/model_evaluation.md). Decisions:
[ADR 0001](adr/0001-fraud-signal-gate.md), [ADR 0002](adr/0002-model-search.md).

## Contents

1. [Running it](#1-running-it)
2. [What a run does](#2-what-a-run-does)
3. [Design decisions](#3-design-decisions)
4. [How the model was chosen: evaluation rigor](#4-how-the-model-was-chosen-evaluation-rigor)
5. [Results](#5-results)
6. [What the policy step must know](#6-what-the-policy-step-must-know)
7. [Tests](#7-tests)
8. [Limitations](#8-limitations)

## 1. Running it

```bash
make label-signal   # signal gate: what is learnable (about 6 min)
make train          # fit, compare on frozen sets, log to MLflow, write model + report (1 min)
make model-search   # tuned LightGBM / logistic regression vs the calibrator (about 10 min)
make gold           # rescore gold.transaction_scores and the serving slice with the model
uv run --group ml mlflow ui --backend-store-uri sqlite:///mlruns/mlflow.db   # browse runs
```

`make train` needs gold (the frozen sets, `score_calibration` and `channel_costs`). MLflow is
in the `ml` dependency group, so the API image does not carry it.

## 2. What a run does

```mermaid
flowchart TD
    G[(gold.transaction_features<br/>train: process_date < 2025-07-01)] --> C[Counts per distinct score<br/>+ unscored]
    C --> R[raw_score: score / 100]
    C --> H[histogram_baseline<br/>gold.score_calibration]
    C --> I[isotonic]
    C --> B[bayes_blocks]
    F[(eval/frozen<br/>hash checked)] --> E[Probability metrics<br/>+ contact-rule simulation]
    R & H & I & B --> E
    E --> M[(MLflow: one run per calibrator)]
    E --> S{Best validation<br/>net benefit}
    S --> J[models/...v1.json<br/>committed]
    S --> P[reports/model_evaluation.md]
    J --> T[make gold: transaction_scores<br/>p_fraud, p_fraud_low, p_fraud_high]
    T --> V[(serving slice)]
```

## 3. Design decisions

### 3.1 A gate before any model

**Decision:** before training, `make label-signal` checks what the data can teach: the score
as a ranker, LightGBM on behavior with a bootstrap interval and a permutation null, Isolation
Forest, a forward-selection step from the score, and the transcript labels.

**Why:** the plan assumed behavioral features carry fraud signal. They do not (PR-AUC at the
base rate, p = 0.38), and the intent classifier fallback had no label either. Training first
would have produced a model at chance with a polished report. Decision record:
[ADR 0001](adr/0001-fraud-signal-gate.md).

### 3.2 The score is at the ceiling, so the model is the score-to-probability map

**Decision:** the learned component is a calibrator of `fraud_score`.

**Why:** legitimate scores never exceed 30.00, so frauds above it are separable, and frauds
below it look exactly like legitimate transactions. The best possible ROC-AUC on scored rows
is 0.834 (validation) and 0.862 (test); the score reaches 0.823 and 0.856. No model can rank
better. What the system still needs is a **probability**: the contact rule multiplies it by
the amount, and a 0–100 score read as a percentage loses USD 238k on validation (§4).

### 3.3 Bayesian blocks: the data places the edges

**Decision:** split the score axis into blocks of constant fraud rate, chosen by maximizing
the Beta-Binomial marginal likelihood minus a penalty per block (optimal partitioning by
dynamic programming, as in Bayesian Blocks). Each block's rate gets a Beta posterior.

**Why:**

- The fixed 5-point histogram of the baseline puts an edge at 30 and mixes the 609
  legitimate transactions at exactly 30.00 with the pure-fraud scores above it: it predicts
  0.24 for 30.01–34.99, where every train transaction is fraud. The blocks put the edge at
  30.00 / 30.01 on their own.
- The posterior gives an interval as well as a mean. The policy needs the interval to
  abstain ("AI should not be autonomous just because it can be").
- Exact and cheap: the Beta prior is conjugate, so there is no sampling (no MCMC), and the
  dynamic program over 4,469 distinct train scores takes under a second.

**Choices inside it:**

| Choice | Value | Why |
|--------|-------|-----|
| Prior | Jeffreys Beta(½, ½) | Uninformative; avoids 0 and 1 in pure blocks |
| Penalty per block | log N (14.9 nats) | BIC-like; set before seeing validation, so validation stays clean for selection |
| Unscored transactions | One more block | 20% of rows, their own fraud rate (0.105%) |
| Interval | 95% equal-tailed | Standard; stored in the model file |

**Rejected:** a finer fixed histogram (where to put the edges is the question, and fixed
edges cannot answer it); isotonic regression as the production model (equal decisions,
slightly worse validation numbers, and no interval); LightGBM on the score (it fits ties and
loses PR-AUC, §4 of the gate report).

### 3.4 Selection on validation, report on test

**Decision:** four calibrators (raw score, histogram baseline, isotonic, Bayesian blocks) are
fitted on train and compared on the frozen sets. The one with the highest **validation** net
benefit (then lowest log loss) is selected. Test numbers are reported and never used to choose.

**Why:** the frozen sets are checked against their committed SHA-256 before use, so every run
and every model is compared on the same rows. Net benefit is the criterion because the
probability exists to drive the contact decision; log loss breaks ties. The paired bootstrap
(§5) shows that Bayesian blocks, isotonic and the histogram are statistically tied on net
benefit; Bayesian blocks is kept for its interval and learned edges (ADR 0002).

### 3.5 The model is a committed JSON file

**Decision:** `make train` writes `models/fraud_calibrator_bayes_blocks_v1.json` (block edges,
counts, prior, penalty, train cutoff); it is committed. `make gold` scores every transaction
from it, through `BayesianBlocksCalibrator` itself, so gold, the API and the evaluation use the
same numbers. Without a model file, gold falls back to the histogram baseline.

**Why:**

- It holds aggregated counts only (no rows, no PII), so it can live in git like the policies.
- It is deterministic (no timestamps): an unchanged retrain leaves git clean, and a changed
  one shows up as a reviewable diff.
- Gold refuses a model trained with a different train cutoff than `settings.yaml`.
- `score_calibration` (the baseline) is still built every run: it is the comparator.

### 3.6 MLflow for runs, git for the model

**Decision:** one MLflow run per calibrator (parameters, validation and test metrics, frozen
set hashes, cost assumptions version, the blocks as an artifact), stored in `mlruns/` with a
SQLite backend (git-ignored).

**Why:** MLflow shows how the candidates compare across runs. The model the system uses is
the committed file, so a fresh clone does not need anyone's `mlruns/`.

## 4. How the model was chosen: evaluation rigor

The calibrated score was not the first idea; it is what survived. Each step below could have
overturned it, and each ran on the same frozen sets with the same metric.

| Step | Question | Method | Outcome | Evidence |
|---|---|---|---|---|
| 1. Sanity | Are the features point-in-time and the splits sound? | Row counts vs silver, fraud rate per split, 18 features recomputed by hand from silver | All match | `reports/label_signal.md` §1 |
| 2. Signal gate | Is there learnable signal beyond the score? | LightGBM, Isolation Forest, bootstrap and permutation null, forward selection | Behavior at chance (p = 0.38) | `reports/label_signal.md` |
| 3. Ceiling | Is the score's ROC-AUC low? | Best possible ranking given the data | Score at the ceiling (0.823 vs 0.834) | `reports/label_signal.md` §2b |
| 4. Calibrators | Which map from score to probability? | 4 calibrators + no skill, selected on validation, paired bootstrap | Calibration worth +$227k over no skill; calibrators tied | `reports/model_evaluation.md` |
| 5. Tuned search without the score | Can behavior beat the score if it is the challenge's baseline? | LightGBM with Bayesian optimization and random search, logistic regression | Same as no skill; $226–274k below the score | `reports/model_search.md` A |
| 6. Tuned hybrid | Can ML fix the range where the score is weak? | Same search, only score ≤ 30 and unscored | Tuning gain on tune vanished on validation and test | `reports/model_search.md` B |

### 4.1 Leakage controls

- **Time splits only.** Train before 2025-07-01, validation to 2025-12-31, test to 2026-06-17;
  for tuning, train is split again in time (fit, then its last 6 months as tune).
- **Validation chooses, test reports.** Hyperparameters are chosen on tune, models on
  validation; test is never used to choose anything.
- **Frozen sets are hash-checked** before every evaluation, so every model sees the same rows.
- **Features are point-in-time** (gold invariance test plus the hand recomputation).
- **Not ruled out by the data:** whether `fraud_score` is known at authorization time. It is a
  stated assumption (ADR 0002).

### 4.2 The metric that decides: net benefit in USD

ROC-AUC and PR-AUC say how well a model ranks; the system needs to know what its decisions
are worth. Every model's probability goes through the contact rule and is scored in dollars:

```
contact            if  p × amount_usd  >  contact cost (SMS, 0.106)  +  friction (2.00, synthetic)
net benefit (USD)  =   Σ amount of contacted frauds          (loss avoided)
                     − contact cost × contacts
                     − friction × legitimate customers contacted
```

| Companion metric | Definition | Why |
|---|---|---|
| Share of oracle | Net benefit / net benefit of contacting exactly the frauds | Comparable across splits and subsets |
| No-skill reference | Every transaction gets the train fraud rate | Large amounts clear the hurdle even at the base rate, so knowing nothing already earns money (35–42% of the oracle). A model's value is what it adds above that |
| Paired bootstrap | Resample transactions, recompute both models' net benefit on the same rows, 95% interval of the difference | Net benefit hinges on a few hundred frauds with large amounts; without an interval a $20k "gain" can be noise |
| Dollar-weighted AP | Average precision with each transaction weighted by its amount | Ranking the expensive frauds first; used in the tuning objective with the share of oracle |

It is an **offline simulation**: costs are synthetic (`policies/cost_assumptions_v1.yaml`) and a
contacted fraud is assumed to be fully avoided.

### 4.3 What the tuned search showed

- **Tuning can create an illusion.** In the hybrid, Bayesian optimization found a 38% gain on
  the tune split (331 frauds) that vanished on validation and test: with few positives, the
  best of 60 configurations is partly the luckiest.
- **The optimizer pointed at the answer.** Its best configuration was the most restricted one
  in the search space (4 leaves, 63 trees, 2,614 rows per leaf): close to a constant.
- **Smarter search does not help on a flat objective.** Without the score, random search beat
  TPE (0.426 vs 0.421); there was nothing for a Bayesian optimizer to exploit.

## 5. Results

From [`reports/model_evaluation.md`](../reports/model_evaluation.md) (offline; the decision
outcomes are a simulation with synthetic costs):

| Calibrator | Validation log loss | Validation net benefit (USD) | Test log loss | Test net benefit (USD) |
|---|---:|---:|---:|---:|
| no_skill (train fraud rate) | 0.00749 | 487,333 | 0.00708 | 335,426 |
| raw_score | 0.13733 | −237,514 | 0.13740 | −343,192 |
| histogram_baseline | 0.00384 | 692,234 | 0.00334 | 585,293 |
| isotonic | 0.00376 | 708,283 | 0.00326 | 589,186 |
| **bayes_blocks** | **0.00375** | **714,345** | **0.00325** | **591,159** |
| oracle (contacts only frauds) | | 1,166,149 | | 952,824 |

- Calibration is what makes the score usable: the raw score loses money; any calibrator makes it.
- **The calibrated score adds +$227k (validation) and +$256k (test) over knowing nothing**,
  with 95% intervals far from 0 ([+171k, +282k] and [+206k, +304k]).
- Bayesian blocks has the best point estimates (+$22k validation, +$6k test over the
  histogram, mostly from the 30.01–34.99 range the histogram underestimates), but the paired
  bootstrap **cannot distinguish** it from the histogram or isotonic regression (intervals
  include 0). It is kept for its interval and learned edges, not for a proven gain.
- The gap to the oracle is the frauds no model can see (scores ≤ 30 and unscored).
- Recall is similar across segments, countries and age bands (0.54–0.67 on 36–357 frauds per
  group), and the legitimate contact rate is 0.100–0.101 everywhere (report, last table).

## 6. What the policy step must know

1. **Most legitimate contacts come from a bet on friction.** Below a score of 30 the
   probability is 0.0003, so the expected-value rule contacts only when the amount exceeds
   about USD 6,800 (USD 2,000 when unscored). At the synthetic USD 2 friction that adds 74,673
   legitimate contacts on validation to catch 39 more frauds. Validation sensitivity:

   | Friction (USD) | Contacts | Frauds contacted | Legit contacted |
   |---:|---:|---:|---:|
   | 0.5 | 216,582 | 485 / 699 | 216,097 |
   | 2 | 75,086 | 413 / 699 | 74,673 |
   | 5 | 16,586 | 383 / 699 | 16,203 |
   | 10 | 1,556 | 374 / 699 | 1,182 |
   | 20 | 374 | 374 / 699 | 0 |

   From about USD 10 the rule contacts only the scores above 30.00 (all fraud). The policy
   needs a contact cap, or a minimum probability, rather than relying on one synthetic number.

2. **The 95% interval is narrow, so interval-based abstention catches near-ties.** It
   straddles the break-even only for amounts close to it (16,374 validation transactions, 9
   frauds). There both decisions cost about the same, so sending them to a human costs more
   than it saves. Abstention is more useful for high amounts, repeat complainers and failed
   tools, as the build plan says.

3. **Transactions without a score (20%)** get p = 0.00105 and are never proactive targets
   below about USD 2,000. The reactive path covers them.

## 7. Tests

| File | Covers |
|------|--------|
| `tests/test_calibration.py` | Blocks find a step edge and merge a flat rate; boundary scores go to the right block; interval contains the mean and narrows with data; JSON round trip; isotonic is monotone; baselines |
| `tests/test_train.py` | Expected-value rule outcomes, with and without sample weights; per-row net benefit sums to the total; abstention counting; ECE of a perfect calibrator is 0; selection uses validation, not test; the no-skill model ignores the score |
| `tests/test_gold.py` | Gold scores with the model file (mean and interval per block, unscored block) and refuses a model with a different train cutoff |
| `tests/test_serving.py` | The slice carries the interval columns |

## 8. Limitations

- **One score, one signal.** Nothing in the data improves on `fraud_score`, tuned or not;
  frauds scored ≤ 30 or unscored stay invisible to the proactive path.
- **`fraud_score` is assumed known at authorization time.** If it is not allowed as an input,
  the best model is no skill (ADR 0002).
- **The model search uses 30 trials per sampler and one seed.** Its conclusion rests on the
  bootstrap intervals, not on the exact configuration found.
- **The simulation assumes a contacted fraud's loss is fully avoided** and uses one SMS per
  contact. The policy step owns channel choice and these assumptions.
- **Net benefit is in assumed USD**, with the synthetic friction of
  `policies/cost_assumptions_v1.yaml`.
- **The penalty (log N) is a convention.** With 2.4M rows in one block and 1,651 in the
  other, any reasonable penalty gives the same two blocks.
- **No drift monitoring yet:** the block rates are stable across years (fraud rate
  0.088–0.102%), but nothing alerts if a new score distribution appears.
