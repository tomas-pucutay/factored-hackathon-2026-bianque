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


def test_dedupe_keeps_latest_ingested_row(tmp_path):
    import pandas as pd

    from bianque.pipeline.silver import build_table, connect

    settings = make_settings(tmp_path)
    cols = ["id", "score", "amount", "active", "country", "langs"]
    write_bronze(tmp_path, "t", "old", [("a", "1.0", None, None, None, None)], cols)
    write_bronze(tmp_path, "t", "new", [("a", "2.0", None, None, None, None)], cols)
    # Make "new" the later ingestion
    path = tmp_path / "bronze" / "t" / "new.parquet"
    df = pd.read_parquet(path)
    df["_ingested_at"] = pd.Timestamp("2026-10-02", tz="UTC")
    df.to_parquet(path, index=False)

    con = connect(settings)
    result = build_table(con, settings, CONTRACT)

    assert (result.rows_in, result.duplicates, result.rows_out) == (2, 1, 1)
    assert con.sql(f"SELECT score FROM '{tmp_path}/silver/t/*.parquet'").fetchall() == [(2,)]


CHILD = parse_contract(
    {
        "table": "child",
        "kind": "dimension",
        "primary_key": ["id"],
        "dedupe_order": ["_ingested_at DESC"],
        "columns": {
            "id": {"type": "VARCHAR", "nullable": False},
            "parent_q": {"type": "VARCHAR"},
            "parent_n": {"type": "VARCHAR"},
        },
        "foreign_keys": [
            {"column": "parent_q", "references": "t.id"},
            {"column": "parent_n", "references": "t.id", "on_orphan": "nullify"},
        ],
    }
)


def test_foreign_keys_quarantine_or_nullify_orphans(tmp_path):
    from bianque.pipeline.silver import build_table, connect

    settings = make_settings(tmp_path)
    parent_cols = ["id", "score", "amount", "active", "country", "langs"]
    write_bronze(tmp_path, "t", "p", [("a", None, None, None, None, None)], parent_cols)
    write_bronze(
        tmp_path,
        "child",
        "c",
        [
            ("ok", "a", "a"),  # both keys valid
            ("orphan", "zzz", "a"),  # quarantine policy -> quarantined
            ("nulled", "a", "zzz"),  # nullify policy -> kept with NULL
            ("no_key", None, None),  # NULL keys are not orphans
        ],
        ["id", "parent_q", "parent_n"],
    )

    con = connect(settings)
    build_table(con, settings, CONTRACT)
    result = build_table(con, settings, CHILD)

    assert result.quarantined == 1
    assert result.nullified == {"parent_n": 1}
    rows = con.sql(
        f"SELECT id, parent_n FROM '{tmp_path}/silver/child/*.parquet' ORDER BY id"
    ).fetchall()
    assert rows == [("no_key", None), ("nulled", None), ("ok", "a")]
    q = con.sql(
        f"SELECT id, _reason FROM '{tmp_path}/silver/_quarantine/child/*.parquet'"
    ).fetchall()
    assert q == [("orphan", "orphan:parent_q")]


def test_missing_parent_fails_clearly(tmp_path):
    from bianque.pipeline.silver import build_table, connect

    settings = make_settings(tmp_path)
    write_bronze(tmp_path, "child", "c", [("x", None, None)], ["id", "parent_q", "parent_n"])
    with pytest.raises(FileNotFoundError, match="build its parents first"):
        build_table(connect(settings), settings, CHILD)


def test_table_sql_adds_derived_columns_and_is_checked(tmp_path):
    from dataclasses import replace

    from bianque.pipeline.contracts import _column
    from bianque.pipeline.silver import build_table, connect

    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    settings = replace(make_settings(tmp_path), silver_sql_dir=sql_dir)
    contract = replace(CONTRACT, derived={"score_x2": _column("score_x2", {"type": "INTEGER"})})
    cols = ["id", "score", "amount", "active", "country", "langs"]
    write_bronze(tmp_path, "t", "f", [("a", "3.0", None, None, None, None)], cols)
    con = connect(settings)

    (sql_dir / "t.sql").write_text(
        "WITH k AS (SELECT 2 AS f) SELECT input.*, score * k.f AS score_x2 FROM input, k;"
    )
    build_table(con, settings, contract)
    assert con.sql(f"SELECT score_x2 FROM '{tmp_path}/silver/t/*.parquet'").fetchall() == [(6,)]

    (sql_dir / "t.sql").write_text("SELECT * FROM input")  # forgets the derived column
    with pytest.raises(ValueError, match=r"missing \['score_x2'\]"):
        build_table(con, settings, contract)


@pytest.mark.parametrize("key", ["short-key", "k" * 64, "x" * 100])  # <, =, > one block
@pytest.mark.parametrize("value", ["ana@mail.com", "Díaz Pérez", ""])
def test_hmac_sql_matches_python_hmac(key, value):
    import hashlib
    import hmac

    from bianque.pipeline.silver import hmac_sql

    got = duckdb.sql(f"SELECT {hmac_sql('v', key)} FROM (SELECT ? AS v)", params=[value])
    expected = hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()
    assert got.fetchone()[0] == expected


@pytest.mark.parametrize(
    ("dob", "band"),
    [
        ("2008-06-18", "<18"),  # 17 the day before the 18th birthday
        ("2008-06-17", "18-24"),
        ("2001-06-18", "18-24"),
        ("2001-06-17", "25-34"),
        ("1961-06-17", "65+"),
        (None, None),
    ],
)
def test_age_band_at_reference_date(dob, band):
    from datetime import date

    from bianque.pipeline.silver import age_band_sql

    expr = age_band_sql("d", date(2026, 6, 17), (18, 25, 35, 45, 55, 65))
    got = duckdb.sql(f"SELECT {expr} FROM (SELECT ?::DATE AS d)", params=[dob]).fetchone()[0]
    assert got == band


PII_CONTRACT = parse_contract(
    {
        "table": "p",
        "kind": "dimension",
        "primary_key": ["id"],
        "dedupe_order": ["_ingested_at DESC"],
        "columns": {
            "id": {"type": "VARCHAR", "nullable": False},
            "email": {"type": "VARCHAR"},
            "date_of_birth": {"type": "DATE"},
            "score": {"type": "INTEGER"},
        },
        "derived": {"age_band": {"type": "VARCHAR"}},
        "pii": {"hash": ["email"], "age_band": ["date_of_birth"]},
    }
)


def test_pii_is_removed_from_silver_and_quarantine(tmp_path):
    import hashlib
    import hmac

    from bianque.pipeline.silver import build_table, connect

    settings = make_settings(tmp_path)
    cols = ["id", "email", "date_of_birth", "score"]
    rows = [("ok", "ana@mail.com", "1990-01-01", "1.0"), ("bad", "bob@mail.com", "1980-01-01", "x")]
    write_bronze(tmp_path, "p", "f", rows, cols)

    con = connect(settings)
    build_table(con, settings, PII_CONTRACT, pii_key="secret")

    token = hmac.new(b"secret", b"ana@mail.com", hashlib.sha256).hexdigest()
    silver = f"'{tmp_path}/silver/p/*.parquet'"
    assert "date_of_birth" not in con.sql(f"SELECT * FROM {silver}").columns
    assert con.sql(f"SELECT id, email, age_band FROM {silver}").fetchall() == [
        ("ok", token, "35-44")
    ]

    quarantine = f"'{tmp_path}/silver/_quarantine/p/*.parquet'"
    assert "date_of_birth" not in con.sql(f"SELECT * FROM {quarantine}").columns
    assert con.sql(f"SELECT id, email, age_band FROM {quarantine}").fetchall() == [
        ("bad", hmac.new(b"secret", b"bob@mail.com", hashlib.sha256).hexdigest(), "45-54")
    ]


def test_pii_contract_without_key_fails(tmp_path):
    from bianque.pipeline.silver import build_table, connect

    settings = make_settings(tmp_path)
    write_bronze(
        tmp_path, "p", "f", [("a", None, None, None)], ["id", "email", "date_of_birth", "score"]
    )
    with pytest.raises(ValueError, match="need a hash key"):
        build_table(connect(settings), settings, PII_CONTRACT)


def test_write_parquet_replaces_only_the_given_months(tmp_path):
    from bianque.pipeline.silver import write_parquet

    con = duckdb.connect()
    out = tmp_path / "f"
    rows = "SELECT * FROM (VALUES (1, DATE '2024-01-05'), (2, DATE '2024-02-05'), (3, DATE '2024-03-05')) v(id, d)"
    write_parquet(con, rows, out, "d")

    # Rewrite Feb with new content and empty March; January must stay untouched.
    jan_before = sorted(p.name for p in (out / "process_month=2024-01").iterdir())
    write_parquet(
        con, "SELECT 20 AS id, DATE '2024-02-07' AS d", out, "d", months={"2024-02", "2024-03"}
    )

    got = con.sql(
        f"SELECT id, process_month FROM read_parquet('{out}/**/*.parquet', hive_partitioning=true) ORDER BY id"
    )
    assert got.fetchall() == [(1, "2024-01"), (20, "2024-02")]
    assert sorted(p.name for p in (out / "process_month=2024-01").iterdir()) == jan_before
    assert not (out / "process_month=2024-03").exists()


def test_write_parquet_rejects_rows_outside_the_given_months(tmp_path):
    from bianque.pipeline.silver import write_parquet

    con = duckdb.connect()
    with pytest.raises(ValueError, match="other months"):
        write_parquet(
            con, "SELECT 1 AS id, DATE '2024-05-01' AS d", tmp_path / "f", "d", months={"2024-01"}
        )
