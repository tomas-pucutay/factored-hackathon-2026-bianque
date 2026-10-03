# ADR 0001: The learned component is a calibrator of the bank's fraud score

- **Status:** accepted, 2026-10-04
- **Evidence:** [`reports/label_signal.md`](../../reports/label_signal.md) (`make label-signal`)

## Context

The build plan called for a fraud model (LightGBM on point-in-time behavioral features)
compared with two baselines, the bank's `fraud_score` and an Isolation Forest. A signal
gate was run first, on the out-of-time splits of the frozen evaluation sets, because the
whole plan depends on it.

The gate's decision table:

| Result | Meaning | Action |
|--------|---------|--------|
| `fraud_score` ROC-AUC ≈ 1.0 | It is a copy of the label | Keep it out of features |
| Model PR-AUC well above the base rate | Real signal | Continue with the plan |
| Model PR-AUC ≈ base rate | No learnable signal | Switch to the fallback and write an ADR |

What the data says:

- `fraud_score` is a real, imperfect signal, not a label copy: ROC-AUC 0.71 (validation) and
  0.72 (test), 0.82–0.86 on the 80% of transactions that have it.
- A model on behavioral features has PR-AUC 0.00096 on validation for a base rate of 0.00094,
  inside the permutation null (p = 0.38). Isolation Forest is also at chance.
- Behavior adds nothing to the score, even where the score is weak: not on unscored
  transactions, not in the ambiguous 25–40 band, not in a forward-selection step (every gain
  is under a tenth of the score's own bootstrap interval). Combining them lowers PR-AUC.
- The planned fallback, an intent classifier, has no label in the data: 42 distinct customer
  texts, `detected_intents` is constant, and a text model equals the majority class.

## Decision

1. **The learned component is a calibrator** that maps the bank's `fraud_score` (and whether
   it is missing) to a probability of fraud. It is fitted on train only and compared on the
   frozen validation and test sets with two baselines: the current histogram calibration
   (`baseline_fraud_score_v1`) and a raw threshold on `fraud_score`. It is judged on
   calibration (Brier score, reliability by score range) and on the cost of the contact
   decisions it drives. Ranking metrics alone cannot improve, because it is a function of
   one score.
2. **Behavioral features are not model inputs.** `gold.transaction_features` stays: it is
   point-in-time, tested, and feeds the agent's context and the fairness breakdowns.
3. **Abstention uses a confidence band:** transactions whose probability falls in the
   uncertain middle range go to a human instead of an automated decision. On this data that
   is the 30–35 score range (calibrated 0.24, observed 0.19 out of sample; below 30 it is
   0.0003, from 35 up 0.99). The band edges are set on validation.
4. **No intent classifier is trained on this dataset.** The LLM extracts intent into
   validated JSON, as designed. Its evaluation uses team-generated, labeled Spanish and
   Portuguese scenarios.

## Consequences

- The separation of concerns is unchanged: the model outputs a calibrated probability and
  the policy decides. Only what produces the probability changes.
- The claim to the judges changes from "our model detects fraud better" to "we measured that
  nothing beats the bank's score on this data, and we made that score usable for an
  expected-value decision". The report is part of the submission.
- Fraud in the 20% of transactions without a score cannot be detected by anything in this
  dataset; those transactions get the low unscored base rate and are never contacted
  proactively. The reactive path (the customer reports the charge) still covers them.
- MLflow tracks the calibrator runs and the baselines on the same frozen sets.

## Alternatives rejected

| Alternative | Why not |
|-------------|---------|
| Train LightGBM on behavior anyway and report it | It would be a model at chance presented as a learned component |
| LightGBM on behavior + `fraud_score` | Lower PR-AUC than the score alone (0.528 vs 0.538 on validation) |
| Intent classifier on transcripts | No valid label (§5 of the report) |
| Intent classifier on team-generated utterances | Labels would be synthetic. Not needed for the workflow, since the LLM extracts intent |
| Deeper feature subset search or PCA / MDS projections | Univariate and multivariate signal are both at noise level, so a search or a projection has nothing to find |
| Probe other targets (campaign response, SLA breach) | Not a priority before the deadline; noted as remaining work |
