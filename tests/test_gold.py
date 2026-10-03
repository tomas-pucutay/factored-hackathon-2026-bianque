import duckdb
import pytest
from conftest import lake_settings, write_silver

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
