"""Serving slice: a small, self-contained DuckDB file that the deployed API reads.

The API cannot reach the lake, so gold ships a slice of it: a few hundred customers chosen
deterministically, everything the tools and the agent need about them, and the small
reference tables (agents, channel costs, cost assumptions).

Customer selection (ordered by md5(customer_id), so the same data gives the same slice):
  1. proactive targets: a transaction in the last 30 days with p_fraud >= 0.5
  2. customers with a charge dispute (Transactions / Fees) in the last 90 days
  3. the rest of settings.serving_customers from all customers
Each group takes at most a third of the slice; the remainder fills group 3.

No table in the slice carries is_fraud: the app must decide from scores, never from labels.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import duckdb

from bianque.config import Settings
from bianque.pipeline.silver import lit

TARGET_P_FRAUD = 0.5
RECENT_TX_DAYS = 30
RECENT_DISPUTE_DAYS = 90
SLICE_TX_DAYS = 90


def selection_sql(n: int) -> str:
    third = n // 3
    return f"""
        WITH as_of AS (SELECT max(as_of_date) AS d FROM customer_360),
        targets AS (
            SELECT DISTINCT s.customer_id, 'proactive_target' AS reason
            FROM transaction_scores AS s, as_of
            WHERE s.p_fraud >= {TARGET_P_FRAUD}
              AND s.process_date > as_of.d - INTERVAL {RECENT_TX_DAYS} DAYS
            ORDER BY md5(s.customer_id) LIMIT {third}
        ),
        disputes AS (
            SELECT DISTINCT o.customer_id, 'recent_dispute' AS reason
            FROM dispute_outcomes AS o, as_of
            WHERE o.is_charge_dispute
              AND o.process_date > as_of.d - INTERVAL {RECENT_DISPUTE_DAYS} DAYS
              AND o.customer_id NOT IN (SELECT customer_id FROM targets)
            ORDER BY md5(o.customer_id) LIMIT {third}
        ),
        picked AS (SELECT * FROM targets UNION ALL SELECT * FROM disputes),
        sample AS (
            SELECT customer_id, 'sample' AS reason FROM customer_360
            WHERE customer_id NOT IN (SELECT customer_id FROM picked)
            ORDER BY md5(customer_id) LIMIT {n} - (SELECT count(*) FROM picked)
        )
        SELECT * FROM picked UNION ALL SELECT * FROM sample
    """


def build_serving_slice(con: duckdb.DuckDBPyConnection, settings: Settings) -> dict[str, int]:
    """Write <gold>/serving/serving.duckdb. Needs the gold views registered on `con`."""
    out = settings.gold_root / "serving" / "serving.duckdb"
    tmp = out.with_name(out.name + ".tmp")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp.unlink(missing_ok=True)

    con.execute(
        "CREATE OR REPLACE TEMP TABLE slice_customers AS "
        f"{selection_sql(settings.serving_customers)}"
    )
    tables = {
        "slice_customers": "SELECT * FROM slice_customers",
        "customers": "SELECT c.* FROM customer_360 c SEMI JOIN slice_customers USING (customer_id)",
        "products": "SELECT p.* FROM products p SEMI JOIN slice_customers USING (customer_id)",
        "transactions": f"""
            SELECT t.* EXCLUDE (is_fraud, process_month),
                   s.p_fraud, s.p_fraud_low, s.p_fraud_high, s.model_version, s.scored_at
            FROM transactions t
            SEMI JOIN slice_customers USING (customer_id)
            JOIN transaction_scores s USING (transaction_id)
            WHERE t.process_date > (SELECT max(as_of_date) FROM customer_360)
                                   - INTERVAL {SLICE_TX_DAYS} DAYS""",
        "dispute_outcomes": (
            "SELECT o.* FROM dispute_outcomes o SEMI JOIN slice_customers USING (customer_id)"
        ),
        "agent_routing": "SELECT * FROM agent_routing",
        "channel_costs": "SELECT * FROM channel_costs",
        "cost_assumptions": "SELECT * FROM cost_assumptions",
        "contact_cost_assumptions": "SELECT * FROM contact_cost_assumptions",
    }
    con.execute(f"ATTACH {lit(str(tmp))} AS slice")
    try:
        counts = {}
        for name, query in tables.items():
            con.execute(f"CREATE TABLE slice.{name} AS {query}")
            counts[name] = con.execute(f"SELECT count(*) FROM slice.{name}").fetchone()[0]
        leaked = con.execute(
            "SELECT list(table_name || '.' || column_name) FROM duckdb_columns() "
            "WHERE database_name = 'slice' AND column_name = 'is_fraud'"
        ).fetchone()[0]
        if leaked:
            raise ValueError(f"serving slice must not carry labels: {leaked}")
        con.execute(
            "CREATE TABLE slice.slice_info AS SELECT "
            f"TIMESTAMPTZ {lit(datetime.now(UTC).isoformat())} AS built_at, "
            "(SELECT max(as_of_date) FROM customer_360) AS as_of_date, "
            "(SELECT any_value(model_version) FROM transaction_scores) AS model_version, "
            "(SELECT any_value(assumptions_version) FROM cost_assumptions) AS assumptions_version, "
            f"{counts['slice_customers']} AS n_customers"
        )
    finally:
        con.execute("DETACH slice")
    Path(tmp).replace(out)
    return counts
