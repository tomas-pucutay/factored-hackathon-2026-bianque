import duckdb
import pytest
from conftest import CONTRACTS, lake_settings, write_silver

from bianque.pipeline.gold import build


def test_build_runs_sql_files_in_order_and_registers_outputs(empty_silver, tmp_path):
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "first.sql").write_text("SELECT customer_id, segment FROM customers;")
    (sql_dir / "second.sql").write_text(
        "SELECT count(*) AS n FROM first"
    )  # uses the gold table above
    write_silver(empty_silver, "customers", [{"customer_id": "c1", "segment": "Basic"}])

    settings = lake_settings(empty_silver, gold_sql_dir=sql_dir)
    counts = build(settings, {"first": False, "second": False})

    assert counts == {"first": 1, "second": 1}
    n = duckdb.sql(f"SELECT n FROM '{empty_silver}/gold/second/*.parquet'").fetchone()[0]
    assert n == 1


def test_build_fails_without_silver(tmp_path):
    with pytest.raises(FileNotFoundError, match="run silver first"):
        build(lake_settings(tmp_path), {})


def tx(tid, ts, amount, country="Colombia", channel="POS", merchant=None, fraud=False):
    return {
        "transaction_id": tid,
        "transaction_date": ts,
        "process_date": ts[:10],
        "product_id": "p1",
        "customer_id": "c1",
        "transaction_type": "Purchase",
        "amount": amount,
        "currency": "USD",
        "amount_usd": amount,
        "channel": channel,
        "merchant_name": merchant,
        "transaction_country": country,
        "transaction_status": "Approved",
        "is_fraud": fraud,
        "fraud_score": 10.0,
    }


BASE_TX = [
    tx("t1", "2024-01-01 10:00:00", 10, merchant="m1"),
    tx("t2", "2024-01-01 10:00:00", 20, merchant="m1"),  # same instant as t1
    tx("t3", "2024-01-01 20:00:00", 30, country="Brazil", channel="Web"),
    tx("t4", "2024-01-09 09:00:00", 100, merchant="m2"),
]


def write_customer_and_product(root):
    write_silver(
        root,
        "customers",
        [
            {
                "customer_id": "c1",
                "country": "Colombia",
                "segment": "Basic",
                "age_band": "25-34",
                "registration_date": "2023-01-01 00:00:00",
                "customer_status": "Active",
            }
        ],
    )
    write_silver(
        root,
        "products",
        [
            {
                "product_id": "p1",
                "customer_id": "c1",
                "product_type": "Tarjeta Crédito",
                "opening_date": "2023-06-01",
                "product_status": "Active",
            }
        ],
    )


def features_lake(root, transactions):
    for table in CONTRACTS:
        if not (root / "silver" / table).exists():
            write_silver(root, table, [])
    write_customer_and_product(root)
    write_silver(root, "transactions", transactions)
    write_silver(
        root,
        "complaints",
        [{"complaint_id": "k1", "customer_id": "c1", "creation_date": "2024-01-01 15:00:00"}],
    )
    write_silver(
        root,
        "digital_events",
        [
            {
                "event_id": "e1",
                "customer_id": "c1",
                "event_type": "Login",
                "event_date": "2024-01-01 19:30:00",
            },
            {
                "event_id": "e2",
                "customer_id": "c1",
                "event_type": "Login",
                "event_date": "2024-01-01 20:00:00",
            },
        ],
    )
    build(lake_settings(root), {"transaction_features": True})
    rel = duckdb.sql(
        f"SELECT * FROM read_parquet('{root}/gold/transaction_features/**/*.parquet',"
        " hive_partitioning = true)"
    )
    return {r[0]: dict(zip(rel.columns, r, strict=True)) for r in rel.fetchall()}


def test_transaction_features_are_point_in_time(tmp_path):
    f = features_lake(tmp_path, BASE_TX)

    # Same-instant transactions do not see each other.
    assert (f["t1"]["n_prior_tx"], f["t2"]["n_prior_tx"]) == (0, 0)
    assert f["t1"]["hours_since_prev_tx"] is None

    # t3: both 10:00 transactions are in its 24h window; first time in Brazil and on Web.
    t3 = f["t3"]
    assert (t3["n_tx_24h"], float(t3["sum_usd_24h"])) == (2, 30.0)
    assert (t3["is_new_country"], t3["is_foreign"], t3["is_new_channel"]) == (True, True, True)
    assert t3["is_new_merchant"] is None  # no merchant
    assert t3["n_complaints_90d"] == 1  # complaint at 15:00
    assert (
        t3["n_logins_24h"] == 1
    )  # login at 19:30 counts; the one at 20:00 (same instant) does not

    # t4: more than 7 days after t3, within 30 days of all three.
    t4 = f["t4"]
    assert (t4["n_tx_7d"], t4["n_tx_30d"], t4["n_prior_tx"]) == (0, 3, 3)
    assert float(t4["prior_median_usd"]) == 20.0
    assert float(t4["amount_to_prior_median"]) == 5.0
    assert (t4["exceeds_prior_max"], t4["is_new_country"], t4["is_new_merchant"]) == (
        True,
        False,
        True,
    )

    # Stable attributes and label pass through.
    assert (t4["segment"], t4["product_type"]) == ("Basic", "Tarjeta Crédito")
    assert round(t4["days_since_first_tx"], 2) == 7.96  # observed tenure: since t1/t2
    assert f["t1"]["days_since_first_tx"] is None
    assert t4["is_fraud"] is False and float(t4["source_fraud_score"]) == 10.0


def test_future_transactions_do_not_change_past_features(tmp_path_factory):
    before = features_lake(tmp_path_factory.mktemp("before"), BASE_TX)
    future = [*BASE_TX, tx("t5", "2024-01-09 09:30:00", 999, country="Spain", fraud=True)]
    after = features_lake(tmp_path_factory.mktemp("after"), future)

    for tid in ["t1", "t2", "t3", "t4"]:
        assert after[tid] == before[tid], tid
    assert after["t5"]["n_tx_24h"] == 1  # t5 itself sees t4


def gold_rows(root, table):
    rel = duckdb.sql(
        f"SELECT * FROM read_parquet('{root}/gold/{table}/**/*.parquet', hive_partitioning = true)"
    )
    return [dict(zip(rel.columns, r, strict=True)) for r in rel.fetchall()]


def test_customer_360_snapshot(tmp_path):
    for table in CONTRACTS:
        write_silver(tmp_path, table, [])
    write_customer_and_product(tmp_path)
    write_silver(
        tmp_path,
        "customers",
        [
            {"customer_id": "c1", "country": "Colombia", "segment": "Basic", "age_band": "25-34"},
            {"customer_id": "c2", "country": "Mexico", "segment": "Plus", "age_band": "65+"},
        ],
    )
    write_silver(
        tmp_path,
        "transactions",
        [
            tx("old", "2026-01-01 10:00:00", 10, fraud=True),
            tx("recent", "2026-06-10 10:00:00", 30),
            tx("last", "2026-06-17 10:00:00", 50, country="Brazil"),  # sets as_of_date
        ],
    )
    write_silver(
        tmp_path,
        "complaints",
        [
            {
                "complaint_id": "k1",
                "customer_id": "c1",
                "creation_date": "2026-05-01 10:00:00",
                "process_date": "2026-05-01",
                "status": "Open",
                "reception_channel": "Regulator",
            },
        ],
    )
    build(lake_settings(tmp_path), {"customer_360": False})
    rows = {r["customer_id"]: r for r in gold_rows(tmp_path, "customer_360")}

    c1, c2 = rows["c1"], rows["c2"]
    assert str(c1["as_of_date"]) == "2026-06-17"
    assert (c1["n_tx_total"], c1["n_tx_90d"], float(c1["usd_90d"])) == (3, 2, 80.0)
    assert (c1["n_tx_countries"], c1["observed_tenure_days"]) == (2, 167)
    assert (c1["n_products"], c1["has_credit_card"]) == (1, True)
    assert (c1["n_open_complaints"], c1["has_regulator_complaint"]) == (1, True)
    assert not any("fraud" in col for col in c1)  # no labels in a snapshot

    # A customer without activity: zeros, not NULLs.
    assert (c2["n_tx_total"], c2["n_products"], c2["n_complaints_total"]) == (0, 0, 0)
    assert c2["first_tx_at"] is None


def test_cost_assumptions_are_loaded_with_their_version():
    from pathlib import Path

    from bianque.pipeline.gold import register_cost_assumptions

    con = duckdb.connect()
    assert register_cost_assumptions(con, Path("policies/cost_assumptions_v1.yaml")) == (
        "cost_assumptions_v1"
    )
    rows = dict(
        con.sql(
            "SELECT interaction_type, coalesce(cost_per_minute_usd, cost_per_contact_usd)"
            " FROM contact_cost_assumptions"
        ).fetchall()
    )
    assert set(rows) == {"Inbound Call", "Outbound Call", "Video", "Chat", "Email"}
    assert con.sql("SELECT friction_cost_legit_usd FROM cost_assumptions").fetchone()[0] > 0


def test_cost_assumptions_reject_ambiguous_costs(tmp_path):
    from bianque.pipeline.gold import register_cost_assumptions

    bad = tmp_path / "bad.yaml"
    bad.write_text(
        "version: x\nfriction_cost_legit_usd: 1\n"
        "contact_costs:\n  Chat: {per_minute: 1, per_contact: 2}\n"
    )
    with pytest.raises(ValueError, match="exactly one"):
        register_cost_assumptions(duckdb.connect(), bad)


def send(sid, channel, cost, delivered=True, opened=None, clicked=False, converted=False):
    return {
        "send_id": sid,
        "send_date": "2026-01-01 10:00:00",
        "process_date": "2026-01-01",
        "campaign_id": "cmp1",
        "customer_id": "c1",
        "send_channel": channel,
        "send_status": "Sent" if delivered else "Failed",
        "was_delivered": delivered,
        "was_opened": opened,
        "open_date": "2026-01-01 12:00:00" if opened else None,
        "was_clicked": clicked,
        "had_conversion": converted,
        "send_cost": cost,
    }


def test_channel_costs_use_the_right_denominators(tmp_path):
    for table in CONTRACTS:
        write_silver(tmp_path, table, [])
    write_silver(
        tmp_path,
        "campaign_sends",
        [
            send("s1", "SMS", 0.10, opened=True, clicked=True),
            send("s2", "SMS", 0.10, opened=False),
            send("s3", "SMS", 0.10, delivered=False),  # not delivered: no open tracking
            send("s4", "WhatsApp", 0.05),  # delivered, no open tracking on this channel
            send("s5", "WhatsApp", None),  # cost unknown
        ],
    )
    build(lake_settings(tmp_path), {"channel_costs": False})
    rows = {r["channel"]: r for r in gold_rows(tmp_path, "channel_costs")}

    sms, wa = rows["SMS"], rows["WhatsApp"]
    assert (sms["n_sends"], round(sms["delivery_rate"], 4)) == (3, 0.6667)
    assert (sms["opens_tracked"], sms["open_rate"], sms["click_rate"]) == (True, 0.5, 0.5)
    assert round(float(sms["cost_per_delivered"]), 2) == 0.15  # 0.30 spent / 2 delivered
    assert sms["median_hours_to_open"] == 2.0
    # No response tracking: unknown, not 0.
    assert (wa["opens_tracked"], wa["open_rate"], wa["click_rate"], wa["conversion_rate"]) == (
        False,
        None,
        None,
        None,
    )
    assert (wa["n_sends_with_cost"], float(wa["avg_cost_per_send"])) == (1, 0.05)


def contact(iid, itype, reason, seconds=None, resolved=True):
    return {
        "interaction_id": iid,
        "interaction_date": "2026-01-01 10:00:00",
        "process_date": "2026-01-01",
        "customer_id": "c1",
        "interaction_type": itype,
        "channel": "Phone",
        "contact_reason": reason,
        "reason_category": reason,
        "duration_seconds": seconds,
        "was_resolved": resolved,
        "requires_followup": False,
        "was_escalated": False,
        "has_transcript": False,
        "has_recording": seconds is not None,
    }


def test_service_cost_baseline_prices_minutes_and_flat_contacts(tmp_path):
    for table in CONTRACTS:
        write_silver(tmp_path, table, [])
    write_silver(
        tmp_path,
        "call_center_interactions",
        [
            contact("i1", "Inbound Call", "Queja", seconds=600),  # 10 min x 0.30 = 3.00
            contact("i2", "Inbound Call", "Queja", seconds=120, resolved=False),  # 2 min = 0.60
            contact("i3", "Chat", "Queja"),  # flat 1.00
        ],
    )
    build(lake_settings(tmp_path), {"service_cost_baseline": False})
    rows = {r["interaction_type"]: r for r in gold_rows(tmp_path, "service_cost_baseline")}

    calls, chat = rows["Inbound Call"], rows["Chat"]
    assert (calls["n_contacts"], calls["avg_handle_minutes"]) == (2, 6.0)
    assert round(calls["total_cost_usd"], 2) == 3.60
    assert round(calls["cost_per_contact_usd"], 2) == 1.80
    assert calls["first_contact_resolution_rate"] == 0.5
    assert (chat["median_handle_minutes"], chat["cost_per_contact_usd"]) == (None, 1.0)
    assert {r["assumptions_version"] for r in rows.values()} == {"cost_assumptions_v1"}
    assert calls["n_contacts_without_cost"] == 0


def test_dispute_outcomes_one_row_per_complaint(tmp_path):
    for table in CONTRACTS:
        write_silver(tmp_path, table, [])
    write_customer_and_product(tmp_path)
    write_silver(
        tmp_path,
        "products",
        [
            {"product_id": "p1", "customer_id": "c1", "product_type": "Tarjeta Crédito"},
            {"product_id": "p9", "customer_id": "someone_else", "product_type": "Seguro"},
        ],
    )
    base = {
        "customer_id": "c1",
        "creation_date": "2026-01-01 10:00:00",
        "process_date": "2026-01-01",
    }
    write_silver(
        tmp_path,
        "complaints",
        [
            {
                **base,
                "complaint_id": "k1",
                "category": "Transactions",
                "status": "Closed",
                "reception_channel": "Regulator",
                "sla_breached": True,
                "claimed_amount": 2500,
                "currency": "COP",
                "compensation_granted": 250,
                "affected_product_id": "p9",
                "first_response_date": "2026-01-01 16:00:00",
            },
            {
                **base,
                "complaint_id": "k2",
                "category": "Technical",
                "status": "Open",
                "reception_channel": "App",
                "sla_breached": False,
                "affected_product_id": "p1",
            },
        ],
    )
    build(lake_settings(tmp_path), {"dispute_outcomes": False})
    rows = {r["complaint_id"]: r for r in gold_rows(tmp_path, "dispute_outcomes")}

    k1, k2 = rows["k1"], rows["k2"]
    assert (k1["is_charge_dispute"], k1["is_regulator"], k1["is_resolved"]) == (True, True, True)
    # Amounts are not converted by the (random) currency label.
    assert float(k1["claimed_amount_usd_assumed"]) == 2500
    assert k1["source_currency_label"] == "COP"
    assert (float(k1["compensation_usd_assumed"]), k1["has_compensation"]) == (250, True)
    assert k1["first_response_hours"] == 6.0
    assert (k1["affected_product_is_customers"], k2["affected_product_is_customers"]) == (
        False,
        True,
    )
    assert (k2["is_charge_dispute"], k2["is_resolved"], k2["has_compensation"]) == (
        False,
        False,
        False,
    )
    assert k1["segment"] == "Basic"
