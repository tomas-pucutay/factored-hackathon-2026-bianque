import duckdb
from conftest import CONTRACTS, write_silver
from test_gold import tx, write_customer_and_product

from bianque.pipeline.gold import register_layer
from bianque.quality.checks import table_checks


def checks_by_key(checks):
    return {(c.name, c.column): c for c in checks}


def test_each_contract_rule_catches_its_own_violation(tmp_path):
    for table in CONTRACTS:
        write_silver(tmp_path, table, [])
    write_customer_and_product(tmp_path)
    ok = tx("ok", "2025-03-01 10:00:00", 50)
    rows = [
        ok,
        {**ok, "transaction_id": "dup"},
        {**ok, "transaction_id": "dup"},  # duplicated primary key
        {**ok, "transaction_id": "bad_currency", "currency": "EUR"},  # not allowed
        {**ok, "transaction_id": "bad_score", "fraud_score": 150.0},  # out of range
        {**ok, "transaction_id": "deposit_merchant", "transaction_type": "Deposit",
         "merchant_name": "Shop"},  # structural NULL broken
        {**ok, "transaction_id": "orphan", "customer_id": "c_missing"},  # foreign key
        {**ok, "transaction_id": "wrong_day", "transaction_date": "2025-03-01 03:00:00",
         "process_date": "2025-03-01"},  # before 06:00 belongs to the previous day
        {**ok, "transaction_id": "early", "transaction_date": "2025-03-02 03:00:00",
         "process_date": "2025-03-01"},  # correct: previous process day
        {**ok, "transaction_id": "tie", "transaction_date": "2025-03-03 06:00:00",
         "process_date": "2025-03-02"},  # exactly at the cutoff: either day is accepted
    ]  # fmt: skip
    write_silver(tmp_path, "transactions", rows)
    con = duckdb.connect()
    register_layer(con, tmp_path / "silver")

    got = checks_by_key(table_checks(con, CONTRACTS["transactions"]))

    assert got[("one row per primary key (transaction_id)", None)].failing == 1
    assert got[("allowed values", "currency")].failing == 1
    assert got[("allowed values", "currency")].examples == ["bad_currency"]
    assert got[("range [0, 100]", "fraud_score")].failing == 1
    structural = got[("structural NULL (value where the field does not apply)", "merchant_name")]
    assert structural.failing == 1 and structural.examples == ["deposit_merchant"]
    assert got[("foreign key to customers.customer_id", "customer_id")].failing == 1
    day = got[("process_date follows the business-day rule", None)]
    assert day.failing == 1 and day.examples == ["wrong_day"]
    assert got[("events at exactly the cutoff second (either day accepted)", None)].failing == 1
    assert got[("required value present", "transaction_id")].failing == 0
    failed = {k for k, c in got.items() if not c.passed}
    assert len(failed) == 6  # exactly the six planted violations


def test_missing_rate_is_measured_only_where_the_field_applies(tmp_path):
    for table in CONTRACTS:
        write_silver(tmp_path, table, [])
    write_customer_and_product(tmp_path)
    ok = tx("a", "2025-03-01 10:00:00", 50, merchant="Shop")
    rows = [
        ok,
        {**ok, "transaction_id": "b", "merchant_name": None},  # missing: a purchase
        {**ok, "transaction_id": "c", "transaction_type": "Deposit", "merchant_name": None},
    ]
    write_silver(tmp_path, "transactions", rows)
    con = duckdb.connect()
    register_layer(con, tmp_path / "silver")

    got = checks_by_key(table_checks(con, CONTRACTS["transactions"]))
    missing = got[("missing where the field applies", "merchant_name")]

    assert (missing.failing, missing.total, missing.passed) == (1, 2, True)  # info only
