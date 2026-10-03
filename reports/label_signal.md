# Label signal gate

Run before training, to decide what the learned component can be. Reproduce with
`make label-signal` (about 6 minutes; `src/bianque/evaluation/label_signal.py`). Run on
2026-10-04 on gold as of 2026-06-17. All numbers are **offline measurements** on historical
data.

## Verdict

| Question | Answer | Evidence |
|----------|--------|----------|
| Is `fraud_score` a copy of the label? | **No.** ROC-AUC 0.71–0.72 (0.82–0.86 where it exists) | §2 |
| Can a model learn fraud from behavior? | **No.** PR-AUC equals the base rate; inside the permutation null (p = 0.38) | §2, §3 |
| Does behavior add anything to `fraud_score`? | **No.** Every single-feature addition is within noise; the full combination is worse | §2, §4 |
| Is there a text label for an intent classifier? | **No.** 42 distinct customer texts, a text model equals the majority class, `detected_intents` is constant | §5 |

With the gate's decision table: `fraud_score` is not a label copy, and a model on behavior
sits at the base rate, so there is **no learnable fraud signal beyond the bank's score**. The
consequences are in [`docs/adr/0001-fraud-signal-gate.md`](../docs/adr/0001-fraud-signal-gate.md).

## 1. Sanity checks on gold

| Check | Result |
|-------|--------|
| Rows: `gold.transaction_features` vs `silver.transactions` | 4,425,008 = 4,425,008; all `transaction_id` distinct |
| Fraud rate per split (train < 2025-07-01 ≤ validation < 2026-01-01 ≤ test < 2026-06-18) | train 0.1006% (3,014 of 2,995,552), validation 0.0940% (699), test 0.0880% (603) |
| Leakage spot check | 3 transactions (one fraud, one with complaints and logins, one busy customer), 6 features each recomputed by hand from silver with plain `< t` filters: 18 of 18 equal |

The features recomputed by hand were `n_tx_30d`, `sum_usd_7d`, `hours_since_prev_tx`,
`n_complaints_90d`, `n_logins_24h` and `n_prior_tx`. The test suite already proves the
point-in-time property in general (`tests/test_gold.py`: adding a future transaction changes
no earlier feature).

## 2. Rankers

Train negatives are sampled at 10% (every fraud kept) and weighted back; validation and test
are complete. 34 behavioral features from `gold.transaction_features` (everything except
IDs, dates, the score and the label).

| Ranker | Rows | Fraud | Base rate | ROC-AUC | PR-AUC |
|---|---:|---:|---:|---:|---:|
| [validation] fraud_score, NULL as 0 | 743,934 | 699 | 0.00094 | 0.7100 | 0.5382 |
| [validation] fraud_score, scored rows only | 594,783 | 562 | 0.00094 | 0.8232 | 0.6691 |
| [validation] LightGBM, behavioral features | 743,934 | 699 | 0.00094 | 0.4949 | 0.0010 |
| [validation] LightGBM, behavioral + fraud_score | 743,934 | 699 | 0.00094 | 0.7955 | 0.5279 |
| [validation] Isolation Forest | 743,934 | 699 | 0.00094 | 0.4891 | 0.0009 |
| [test] fraud_score, NULL as 0 | 685,522 | 603 | 0.00088 | 0.7232 | 0.5769 |
| [test] fraud_score, scored rows only | 548,431 | 479 | 0.00087 | 0.8557 | 0.7260 |
| [test] LightGBM, behavioral features | 685,522 | 603 | 0.00088 | 0.5166 | 0.0009 |
| [test] LightGBM, behavioral + fraud_score | 685,522 | 603 | 0.00088 | 0.8252 | 0.5677 |
| [test] Isolation Forest | 685,522 | 603 | 0.00088 | 0.4910 | 0.0009 |
| [validation] LightGBM, behavioral, rows without fraud_score | 149,151 | 137 | 0.00092 | 0.4919 | 0.0010 |
| [validation] fraud_score, ambiguous band 25–40 | 99,253 | 80 | 0.00081 | 0.8227 | 0.6394 |
| [validation] LightGBM, behavioral + fraud_score, band 25–40 | 99,253 | 80 | 0.00081 | 0.8016 | 0.6290 |

Reading it:

- **`fraud_score` is a real but imperfect signal**, not a leaked label: a copy would score
  ROC-AUC ≈ 1. It misses every fraud among the 20% of unscored transactions, and inside
  0–30 fraud and non-fraud overlap (non-fraud scores are uniform on [0, 30), fraud on
  [0, 100); `docs/silver_data_findings.md` §9).
- **Behavioral features rank at chance**, supervised (LightGBM) and unsupervised
  (Isolation Forest), on both splits.
- **Where the score cannot help, behavior does not either:** on unscored rows and in the
  ambiguous band 25–40 the behavioral model is at chance or slightly worse than the score.
- **Adding behavior to the score hurts** (validation PR-AUC 0.538 → 0.528): the trees fit
  noise.
- The ROC-AUC of the combined model is higher than the score's with NULL as 0 (0.80 vs 0.71)
  only because a tree can rank unscored rows by their own base rate instead of placing them
  at 0; PR-AUC, which is what matters at a 0.1% base rate, does not improve.

## 3. Behavioral model vs chance

| PR-AUC | Bootstrap 95% | Permutation null 95% | p-value |
|---:|---|---|---:|
| 0.00096 | [0.00087, 0.00109] | [0.00090, 0.00105] | 0.38 |

200 bootstrap resamples of the validation set and 200 label permutations. The observed
PR-AUC is indistinguishable from a model that ranks at random.

## 4. Forward selection from {fraud_score}

One step of sequential forward selection (feature subset search): start from the score
alone and add each behavioral feature on its own. If `fraud_score` is a Markov blanket of
the label within these features, no addition should help. Small LightGBM (100 trees); the
score alone reaches validation PR-AUC 0.5258 with this model.

| Added feature | PR-AUC gain |
|---|---:|
| amount_usd | +0.0050 |
| amount_to_prior_median | +0.0044 |
| sum_usd_30d | +0.0041 |
| prior_median_usd | +0.0035 |
| hours_since_prev_tx | +0.0033 |
| exceeds_prior_max | +0.0000 |
| sum_usd_24h | −0.0002 |
| days_since_first_tx | −0.0016 |

The largest gain (+0.005) is under a tenth of the bootstrap 95% width of the score's own
PR-AUC (0.064), and the best additions are amount-like features that tie-break within score
bins. With every univariate gain at noise level, a deeper search (more steps, branch and
bound, random subsets) has nothing to find, and projections such as PCA cannot create
signal that the inputs do not have. These were not run.

## 5. Transcript text vs interaction reason

Checked as the fallback for the learned component (an intent classifier):

- 171,321 transcripts, but only **42 distinct customer texts**.
- `detected_intents` is `consulta_general` in every non-NULL row (162,864) and NULL in 8,457.
- TF-IDF + logistic regression from `customer_text` to the interaction's `reason_category`
  (train before 2025-07-01, test from 2026-01-01): **accuracy 0.350, macro F1 0.086**, the
  same as always predicting the majority class (0.350, 6 classes). The same sentence appears
  under different reasons.

The dataset has no label an intent classifier could learn from.

## Limits

- One seed and one hyperparameter set per model; with no signal at the univariate or
  multivariate level, tuning cannot be expected to change the verdict.
- The forward selection is one step; interactions between two or more behavioral features
  are covered by the LightGBM models, which also found nothing.
- 699 validation and 603 test frauds: PR-AUC differences below ~0.06 are not distinguishable.
