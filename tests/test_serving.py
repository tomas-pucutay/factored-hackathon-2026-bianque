from datetime import date

import duckdb
from conftest import CONTRACTS, lake_settings, write_silver

from bianque.pipeline.gold import build

CUSTOMERS = ["c_target", "c_dispute", "c_a", "c_b", "c_c", "c_d"]


def lake(root):
    for table in CONTRACTS:
        write_silver(root, table, [])
    write_silver(
        root,
        "customers",
        [{"customer_id": c, "country": "Colombia", "segment": "Basic"} for c in CUSTOMERS],
    )
    write_silver(
        root,
        "products",
        [
            {"product_id": f"p_{c}", "customer_id": c, "product_type": "Tarjeta Crédito"}
            for c in CUSTOMERS
        ],
    )

    def tx(tid, cust, ts, score, fraud=False):
        return {
            "transaction_id": tid,
            "customer_id": cust,
            "product_id": f"p_{cust}",
            "transaction_date": ts,
            "process_date": ts[:10],
            "transaction_type": "Purchase",
            "amount": 100,
            "currency": "USD",
            "amount_usd": 100,
            "channel": "POS",
            "transaction_country": "Colombia",
            "transaction_status": "Approved",
            "is_fraud": fraud,
            "fraud_score": score,
        }

    train = [tx(f"tr_{i}", "c_a", "2025-01-01 10:00:00", 50.0, fraud=True) for i in range(5)]
    train += [tx(f"tn_{i}", "c_b", "2025-01-02 10:00:00", 10.0) for i in range(50)]
    write_silver(
        root,
        "transactions",
        [
            *train,
            tx("hot", "c_target", "2026-06-10 10:00:00", 60.0, fraud=True),  # recent, high score
            tx("old_target", "c_target", "2025-12-01 10:00:00", 10.0),  # older than 90 days
            tx("last", "c_c", "2026-06-17 10:00:00", 10.0),  # sets as_of_date
        ],
    )
    write_silver(
        root,
        "complaints",
        [
            {
                "complaint_id": "k1",
                "customer_id": "c_dispute",
                "creation_date": "2026-05-01 10:00:00",
                "process_date": "2026-05-01",
                "category": "Transactions",
            }
        ],
    )
    return lake_settings(root, serving_customers=4, eval_train_end=date(2025, 7, 1))


def open_slice(root):
    return duckdb.connect(str(root / "gold" / "serving" / "serving.duckdb"), read_only=True)


def test_serving_slice_selects_targets_disputes_and_a_sample(tmp_path):
    settings = lake(tmp_path)
    counts = build(settings)
    assert counts["serving_customers"] == 4

    con = open_slice(tmp_path)
    reasons = dict(con.sql("SELECT customer_id, reason FROM slice_customers").fetchall())
    assert reasons["c_target"] == "proactive_target"
    assert reasons["c_dispute"] == "recent_dispute"
    assert list(reasons.values()).count("sample") == 2

    # Only selected customers, and only their last 90 days of transactions.
    tx_customers = {
        r[0] for r in con.sql("SELECT DISTINCT customer_id FROM transactions").fetchall()
    }
    assert tx_customers <= set(reasons)
    target_tx = con.sql(
        "SELECT transaction_id, p_fraud > 0.5, p_fraud_low FROM transactions "
        "WHERE customer_id = 'c_target'"
    ).fetchall()
    assert target_tx == [("hot", True, None)]  # the baseline has no interval

    # No labels anywhere in the slice.
    assert (
        con.sql("SELECT count(*) FROM duckdb_columns() WHERE column_name = 'is_fraud'").fetchone()[
            0
        ]
        == 0
    )
    info = con.sql(
        "SELECT model_version, assumptions_version, n_customers FROM slice_info"
    ).fetchone()
    assert info == ("baseline_fraud_score_v1", "cost_assumptions_v1", 4)
    assert con.sql("SELECT count(*) FROM channel_costs").fetchone()[0] == 0  # empty in this lake


def test_serving_slice_is_deterministic(tmp_path):
    settings = lake(tmp_path)
    build(settings)
    first = sorted(open_slice(tmp_path).sql("SELECT customer_id FROM slice_customers").fetchall())
    build(settings)
    second = sorted(open_slice(tmp_path).sql("SELECT customer_id FROM slice_customers").fetchall())
    assert first == second
