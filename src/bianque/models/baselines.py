"""Baseline scorer: the bank's existing fraud_score, calibrated to a probability.

Writes gold.transaction_scores (one row per transaction) and gold.score_calibration (the
bins behind every probability, for the audit trail). The trained model will write its own
model_version into the same table; the proactive scan reads it.

Calibration is histogram binning fitted on the train split only (process_date before
settings.eval_train_end, the same split as the frozen evaluation sets), so validation and test scores are out of sample:
  p(bin) = (frauds_in_bin + PRIOR_WEIGHT * base_rate) / (transactions_in_bin + PRIOR_WEIGHT)
The prior keeps sparse bins from jumping to 0 or 1. Transactions without a fraud_score
(20% of rows) get the rate observed among unscored train transactions.
"""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb

from bianque.config import Settings
from bianque.pipeline.silver import lit, write_parquet

MODEL_VERSION = "baseline_fraud_score_v1"
BIN_WIDTH = 5  # fraud_score is 0-100
PRIOR_WEIGHT = 1.0  # one pseudo-transaction at the base rate: enough for sparse bins


def calibration_sql(train_end: str) -> str:
    """One row per score bin (bin NULL = no score) with its calibrated probability.

    Every bin of the 0-100 grid is present. A bin with no train transactions takes the
    probability of the nearest observed bin (is_filled = true) instead of the base rate,
    which would make an unseen high score look safe.
    """
    n_bins = 100 // BIN_WIDTH
    return f"""
        WITH train AS (
            SELECT
                CASE WHEN fraud_score IS NOT NULL
                     THEN least(floor(fraud_score / {BIN_WIDTH}), {n_bins - 1})::INTEGER
                END AS score_bin,
                is_fraud
            FROM transactions
            WHERE process_date < DATE {lit(train_end)}
        ),
        base AS (SELECT avg(is_fraud::INTEGER) AS base_rate FROM train),
        observed AS (
            SELECT score_bin, count(*) AS n, sum(is_fraud::INTEGER) AS k
            FROM train GROUP BY score_bin
        ),
        grid AS (
            SELECT unnest(range(0, {n_bins}))::INTEGER AS score_bin
            UNION ALL SELECT NULL
        ),
        binned AS (
            SELECT
                g.score_bin,
                coalesce(o.n, 0) AS n_train,
                coalesce(o.k, 0) AS n_fraud_train,
                (o.k + {PRIOR_WEIGHT} * b.base_rate) / (o.n + {PRIOR_WEIGHT}) AS p_observed
            FROM grid AS g
            CROSS JOIN base AS b
            LEFT JOIN observed AS o ON o.score_bin IS NOT DISTINCT FROM g.score_bin
        )
        SELECT
            score_bin,
            score_bin * {BIN_WIDTH} AS score_from,
            (score_bin + 1) * {BIN_WIDTH} AS score_to,
            n_train,
            n_fraud_train,
            coalesce(
                p_observed,
                (SELECT o.p_observed FROM binned AS o
                 WHERE o.p_observed IS NOT NULL AND o.score_bin IS NOT NULL
                   AND binned.score_bin IS NOT NULL
                 ORDER BY abs(o.score_bin - binned.score_bin), o.score_bin LIMIT 1),
                (SELECT base_rate FROM base)
            ) AS p_fraud,
            p_observed IS NULL AS is_filled,
            {lit(MODEL_VERSION)} AS model_version,
            DATE {lit(train_end)} AS trained_before
        FROM binned
    """


def scores_sql(train_end: str, scored_at: str) -> str:
    return f"""
        SELECT
            t.transaction_id,
            t.customer_id,
            t.transaction_date,
            t.process_date,
            t.amount_usd,
            t.fraud_score AS raw_score,
            coalesce(c.p_fraud, (SELECT avg(is_fraud::INTEGER) FROM transactions
                                 WHERE process_date < DATE {lit(train_end)})) AS p_fraud,
            c.model_version,
            TIMESTAMPTZ {lit(scored_at)} AS scored_at,
            t.process_date >= DATE {lit(train_end)} AS is_out_of_sample,
            t.process_month
        FROM transactions AS t
        LEFT JOIN score_calibration AS c ON c.score_bin IS NOT DISTINCT FROM
            CASE WHEN t.fraud_score IS NOT NULL
                 THEN least(floor(t.fraud_score / {BIN_WIDTH}), {100 // BIN_WIDTH - 1})::INTEGER
            END
    """


def build_transaction_scores(con: duckdb.DuckDBPyConnection, settings: Settings) -> int:
    """Fit the calibration on train, score every transaction. Returns rows scored."""
    train_end = settings.eval_train_end.isoformat()
    scored_at = datetime.now(UTC).isoformat()
    cal_out = settings.gold_root / "score_calibration"
    write_parquet(con, calibration_sql(train_end), cal_out, partitioned=False)
    con.execute(
        "CREATE OR REPLACE TEMP VIEW score_calibration AS "
        f"SELECT * FROM read_parquet({lit(str(cal_out / '*.parquet'))})"
    )
    out = settings.gold_root / "transaction_scores"
    write_parquet(con, scores_sql(train_end, scored_at), out, partitioned=True)
    return con.execute(
        f"SELECT count(*) FROM read_parquet({lit(str(out / '**' / '*.parquet'))})"
    ).fetchone()[0]
