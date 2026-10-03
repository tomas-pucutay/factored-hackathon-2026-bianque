import duckdb
import pytest

from bianque.pipeline.contracts import parse_contract
from bianque.pipeline.silver import SchemaError, compare_schema, file_columns, typed_select

CONTRACT = parse_contract(
    {
        "table": "t",
        "kind": "dimension",
        "primary_key": ["id"],
        "dedupe_order": ["_ingested_at DESC"],
        "columns": {
            "id": {"type": "VARCHAR", "nullable": False},
            "score": {"type": "INTEGER"},
            "amount": {"type": "DECIMAL(15,2)"},
            "active": {"type": "BOOLEAN"},
            "country": {"type": "VARCHAR"},
            "langs": {"type": "VARCHAR[]", "split": ", "},
        },
        "value_map": {
            "country": {"México": "Mexico", "nan": None},
            "langs": {"español": "es", "inglés": "en"},
        },
    }
)


def run(rows: list[tuple]) -> list[dict]:
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE src (id VARCHAR, score VARCHAR, amount VARCHAR, active VARCHAR,"
        " country VARCHAR, langs VARCHAR, extra VARCHAR, _source_key VARCHAR,"
        " _source_etag VARCHAR, _ingested_at TIMESTAMP)"
    )
    con.executemany("INSERT INTO src VALUES (?, ?, ?, ?, ?, ?, ?, 'k', 'e', now())", rows)
    rel = con.sql(typed_select(CONTRACT, "src", extra_columns=["extra"]))
    return [dict(zip(rel.columns, r, strict=True)) for r in rel.fetchall()]


def test_casts_and_normalizes_a_clean_row():
    [row] = run([("a", "701.0", "10.50", "True", "México", "español, inglés", "x")])
    assert row["score"] == 701
    assert str(row["amount"]) == "10.50"
    assert row["active"] is True
    assert row["country"] == "Mexico"
    assert row["langs"] == ["es", "en"]
    assert row["extra"] == "x"  # additive column passes through
    assert row["_errors"] == []


def test_value_map_to_null_is_not_an_error():
    [row] = run([("a", None, None, None, "nan", None, None)])
    assert row["country"] is None
    assert row["_errors"] == []


@pytest.mark.parametrize(
    ("values", "error"),
    [
        (("a", "7.5", None, None, None, None, None), "cast:score"),  # real decimal
        (("a", "abc", None, None, None, None, None), "cast:score"),
        (("a", None, "1e999", None, None, None, None), "cast:amount"),
        (("a", None, None, "maybe", None, None, None), "cast:active"),
        ((None, None, None, None, None, None, None), "null:id"),
    ],
)
def test_bad_values_are_reported(values, error):
    [row] = run([values])
    assert error in row["_errors"]


ALL = {"id", "score", "amount", "active", "country", "langs", "_source_key", "_ingested_at"}


def test_additive_column_is_allowed_and_returned():
    assert compare_schema(CONTRACT, {"a.parquet": ALL, "b.parquet": ALL | {"new_col"}}) == [
        "new_col"
    ]


def test_missing_column_breaks_the_build():
    with pytest.raises(SchemaError, match=r"b.parquet: missing \['score'\]"):
        compare_schema(CONTRACT, {"a.parquet": ALL, "b.parquet": ALL - {"score"}})


def test_file_columns_reads_parquet_metadata(tmp_path):
    con = duckdb.connect()
    path = str(tmp_path / "f.parquet")
    con.execute(f"COPY (SELECT 1 AS a, 'x' AS b) TO '{path}' (FORMAT parquet)")
    assert file_columns(con, [path]) == {path: {"a", "b"}}


def make_settings(root):
    from datetime import date
    from pathlib import Path

    from bianque.config import Settings

    return Settings(
        lake_root=Path(root),
        contracts_dir=Path("contracts"),
        silver_sql_dir=Path("sql/silver"),
        late_arrival_days=7,
        age_reference_date=date(2026, 6, 17),
        age_band_edges=(18, 25, 35, 45, 55, 65),
        duckdb_memory_limit="1GB",
        duckdb_threads=2,
    )


def write_bronze(root, table, name, rows, columns):
    from pathlib import Path

    import pandas as pd

    out = Path(root) / "bronze" / table / f"{name}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows, columns=columns, dtype="string")
    df["_source_key"] = f"data/{table}/{name}.csv"
    df["_source_etag"] = name
    df["_ingested_at"] = pd.Timestamp("2026-10-01", tz="UTC")
    df.to_parquet(out, index=False)


def test_build_table_writes_valid_rows_and_quarantines_bad_ones(tmp_path):
    from bianque.pipeline.silver import build_table, connect

    settings = make_settings(tmp_path)
    cols = ["id", "score", "amount", "active", "country", "langs"]
    write_bronze(tmp_path, "t", "f1", [("a", "1.0", "2.50", "True", "México", "español")], cols)
    write_bronze(tmp_path, "t", "f2", [("b", "oops", None, None, None, None)], cols)

    con = connect(settings)
    result = build_table(con, settings, CONTRACT)

    assert (result.rows_in, result.quarantined, result.rows_out) == (2, 1, 1)
    silver = con.sql(f"SELECT id, country FROM '{tmp_path}/silver/t/*.parquet'").fetchall()
    assert silver == [("a", "Mexico")]
    reason = con.sql(
        f"SELECT id, _reason FROM '{tmp_path}/silver/_quarantine/t/*.parquet'"
    ).fetchall()
    assert reason == [("b", "cast:score")]
