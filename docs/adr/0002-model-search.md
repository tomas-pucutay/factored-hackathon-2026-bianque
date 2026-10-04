# ADR 0002: The calibrated fraud_score is the fraud model, after a tuned search failed to beat it

- **Status:** accepted, 2026-10-04
- **Evidence:** [`reports/model_search.md`](../../reports/model_search.md) (`make model-search`),
  [`reports/model_evaluation.md`](../../reports/model_evaluation.md) (`make train`),
  [`reports/label_signal.md`](../../reports/label_signal.md) (`make label-signal`)
- **Follows:** [ADR 0001](0001-fraud-signal-gate.md)

## Context

ADR 0001 made a calibrator of `fraud_score` the learned component, because an untuned model on
behavioral features ranked at chance. Two objections deserved a stronger test before settling:

1. **`fraud_score` may be the challenge's baseline, not an input.** If the organizers provided
   it as the bar to beat, a model that uses it does not beat it. The label is `is_fraud`.
2. **The gate used one untuned model.** A tuned model, a better metric or a model focused on
   the range where the score is weak (≤ 30 and unscored) might find what the gate missed.

## What was tried

Same protocol for every candidate, so the numbers are comparable (details in
[`docs/model_design.md`](../model_design.md) §4):

- Train is split again in time: fit, then tune (its last 6 months). Hyperparameters are chosen
  on tune only. Validation and test are touched once, at the end.
- The objective is the use case: net benefit in USD of the contact rule, as a share of the
  oracle's, plus dollar-weighted ranking (frauds weighted by their amount).
- LightGBM tuned with Bayesian optimization (Optuna TPE) and with random search, 30 trials
  each; logistic regression tuned over C.
- Every final difference in net benefit comes with a paired bootstrap 95% interval.

| Experiment | Question | Result (net benefit vs reference, 95% interval) |
|---|---|---|
| A. No `fraud_score` at all | Can behavior beat the score? | Validation −$225,843 [−274,551, −154,249]; test −$274,207 [−325,986, −225,205]: **worse** |
| A. No `fraud_score` at all | Is behavior better than knowing nothing? | Validation +$3,248 [−14,613, +17,897]; test −$18,491 [−48,039, +3,618]: **no difference** |
| B. Hybrid: ML only where score ≤ 30 or missing | Can ML improve the weak range? | Validation −$4,780 [−24,688, +20,923]; test −$22,519 [−42,824, +1,322]: **no difference** |

Two signs that the data has nothing more to give:

- In the hybrid, tuning looked like a 38% gain on the tune split (combined 0.224 vs 0.162),
  which vanished on validation and test: the search fit the noise of 331 tune frauds.
- Bayesian optimization chose the most restricted model in the search space (4 leaves, 63
  trees, 2,614 rows per leaf). Without the score, random search beat TPE: on a flat, noisy
  objective a smarter search has nothing to exploit.

Among the calibrators of the score (`make train`), Bayesian blocks has the best point
estimates, but its net benefit is **not distinguishable** from the histogram baseline
(validation +$22,110 [−1,877, +54,146]) or isotonic regression. It clearly beats knowing
nothing (validation +$227,012 [+171,333, +282,208]) and the raw score read as a percentage
(+$951,859).

## Decision

1. **The fraud probability is the calibrated `fraud_score` (`bayes_blocks_v1`).** No model on
   the dataset's other variables is used, because none beats a no-skill model.
2. **Bayesian blocks over the histogram and isotonic regression**, even though their net
   benefit is statistically tied: it gives a credible interval (needed to abstain), it learns
   its edges instead of fixing them, and it has the best point estimates on both splits.
3. **Net benefit in USD with a paired bootstrap is the primary model metric**, measured
   against a no-skill reference. ROC-AUC and PR-AUC are reported but do not decide.
4. **Stated assumption:** `fraud_score` is the bank's score, available when the transaction
   is authorized, so using it as an input is legitimate. The data cannot prove its timing.

## Consequences

- The claim is honest and measurable: nothing in this dataset beats the provided score, and
  the contribution is turning it into a decision. Calibrated, it nets +$591k on test where
  the raw score loses $343k and knowing nothing nets +$335k (offline simulation).
- **If the organizers rule that `fraud_score` must not be an input**, the best available
  model is no-skill: probability = base rate, so contacts are driven by the amount alone. The
  system's design would not change (the policy reads a probability), only its value.
- Knowing nothing already captures 35–42% of the oracle's net benefit, purely from the amount
  (large amounts clear the contact hurdle even at the base rate). The calibrated score
  captures 61–62% with less than half the contacts. Every model is reported against that bar.

## Alternatives rejected

| Alternative | Why not |
|---|---|
| Tuned LightGBM without `fraud_score` | Same net benefit as no skill; $226–274k below the calibrated score |
| Hybrid (ML below 30, fixed above) | Not distinguishable from the calibrator; worse point estimates on validation and test |
| Logistic regression | Same as LightGBM in both experiments |
| More trials, other samplers (Hyperband, grid) | The best configurations fit noise; no search creates signal the data lacks |
