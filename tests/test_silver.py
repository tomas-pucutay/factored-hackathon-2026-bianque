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
