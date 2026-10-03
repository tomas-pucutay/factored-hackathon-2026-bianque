"""Baseline scorer: the bank's existing fraud_score, calibrated to a probability.

Writes gold.transaction_scores (one row per transaction) and gold.score_calibration (the
bins behind every probability, for the audit trail). The trained model will write its own
model_version into the same table; the proactive scan reads it.

Calibration is histogram binning fitted on the train split only (transaction_date before
settings.eval_train_end), so validation and test scores are out of sample:
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
    """One row per score bin (bin NULL = no score) with its calibrated probability."""
    return f"""
        WITH train AS (
            SELECT
                CASE WHEN fraud_score IS NOT NULL
                     THEN least(floor(fraud_score / {BIN_WIDTH}), 100 / {BIN_WIDTH} - 1)::INTEGER
                END AS score_bin,
                is_fraud
            FROM transactions
            WHERE transaction_date < DATE {lit(train_end)}
        ),
        base AS (SELECT avg(is_fraud::INTEGER) AS base_rate FROM train)
        SELECT
            score_bin,
            score_bin * {BIN_WIDTH} AS score_from,
            (score_bin + 1) * {BIN_WIDTH} AS score_to,
            count(*) AS n_train,
            sum(is_fraud::INTEGER) AS n_fraud_train,
            (sum(is_fraud::INTEGER) + {PRIOR_WEIGHT} * any_value(base_rate))
                / (count(*) + {PRIOR_WEIGHT}) AS p_fraud,
            {lit(MODEL_VERSION)} AS model_version,
            DATE {lit(train_end)} AS trained_before
        FROM train, base
        GROUP BY score_bin
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
                                 WHERE transaction_date < DATE {lit(train_end)})) AS p_fraud,
            c.model_version,
            TIMESTAMPTZ {lit(scored_at)} AS scored_at,
            t.transaction_date >= DATE {lit(train_end)} AS is_out_of_sample,
            t.process_month
        FROM transactions AS t
        LEFT JOIN score_calibration AS c ON c.score_bin IS NOT DISTINCT FROM
            CASE WHEN t.fraud_score IS NOT NULL
                 THEN least(floor(t.fraud_score / {BIN_WIDTH}), 100 / {BIN_WIDTH} - 1)::INTEGER
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
