"""gold.transaction_scores: what the proactive scan reads.

When settings.fraud_model points to a trained calibrator (make train), every transaction is
scored with it: p_fraud is the posterior mean of its score block and p_fraud_low /
p_fraud_high the credible interval, which the policy uses to abstain. Without a model file the
histogram baseline scores them (no interval). gold.score_calibration (the baseline) is built
in both cases.

The block probabilities come from BayesianBlocksCalibrator itself, so gold and the API use
the same numbers as the evaluation in reports/model_evaluation.md.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from itertools import pairwise

import duckdb
import numpy as np

from bianque.config import Settings
from bianque.models.baselines import (
    build_score_calibration,
    count_rows,
)
from bianque.models.baselines import (
    build_transaction_scores as build_baseline_scores,
)
from bianque.models.calibration import BayesianBlocksCalibrator
from bianque.pipeline.silver import lit, write_parquet


def model_blocks_sql(model: BayesianBlocksCalibrator, level: float) -> str:
    """One row per block: [score_from, score_to) with its probability and interval.

    Edges are the same midpoints the calibrator uses; the first and last blocks are open
    (NULL edge). The unscored block has no edges and is_unscored = true.
    """
    blocks = model.blocks
    cuts = [(a.high + b.low) / 2 for a, b in pairwise(blocks)]
    lows, highs = [None, *cuts], [*cuts, None]
    probe = np.array([b.low for b in blocks] + [np.nan])
    p = model.predict(probe)
    lo, hi = model.interval(probe, level)
    rows = [(lows[i], highs[i], False, p[i], lo[i], hi[i]) for i in range(len(blocks))] + [
        (None, None, True, p[-1], lo[-1], hi[-1])
    ]

    def v(x: float | None) -> str:
        return "NULL::DOUBLE" if x is None else f"{float(x)!r}::DOUBLE"

    values = ",\n".join(
        f"({v(a)}, {v(b)}, {str(u).upper()}, {v(pm)}, {v(pl)}, {v(ph)})"
        for a, b, u, pm, pl, ph in rows
    )
    return f"""
        SELECT * FROM (VALUES {values})
            AS m(score_from, score_to, is_unscored, p_fraud, p_fraud_low, p_fraud_high)
    """


def model_scores_sql(train_end: str, scored_at: str, model_version: str) -> str:
    return f"""
        SELECT
            t.transaction_id,
            t.customer_id,
            t.transaction_date,
            t.process_date,
            t.amount_usd,
            t.fraud_score AS raw_score,
            m.p_fraud,
            m.p_fraud_low,
            m.p_fraud_high,
            {lit(model_version)} AS model_version,
            TIMESTAMPTZ {lit(scored_at)} AS scored_at,
            t.process_date >= DATE {lit(train_end)} AS is_out_of_sample,
            t.process_month
        FROM transactions AS t
        JOIN fraud_model_blocks AS m
          ON CASE WHEN t.fraud_score IS NULL THEN m.is_unscored
                  ELSE NOT m.is_unscored
                       AND (m.score_from IS NULL OR t.fraud_score >= m.score_from)
                       AND (m.score_to IS NULL OR t.fraud_score < m.score_to)
             END
    """


def build_transaction_scores(con: duckdb.DuckDBPyConnection, settings: Settings) -> int:
    """Score every transaction with the trained model, or the baseline. Returns rows."""
    model_path = settings.fraud_model
    if model_path is None or not model_path.exists():
        return build_baseline_scores(con, settings)
    build_score_calibration(con, settings)
    payload = json.loads(model_path.read_text())
    if payload["trained_before"] != settings.eval_train_end.isoformat():
        raise SystemExit(
            f"{model_path} was trained before {payload['trained_before']}, but the train split "
            f"ends at {settings.eval_train_end}; rerun make train"
        )
    model = BayesianBlocksCalibrator.from_dict(payload)
    con.execute(
        "CREATE OR REPLACE TEMP VIEW fraud_model_blocks AS "
        + model_blocks_sql(model, payload["credible_level"])
    )
    out = settings.gold_root / "transaction_scores"
    query = model_scores_sql(
        settings.eval_train_end.isoformat(),
        datetime.now(UTC).isoformat(),
        payload["model_version"],
    )
    write_parquet(con, query, out, partitioned=True)
    return count_rows(con, out)
