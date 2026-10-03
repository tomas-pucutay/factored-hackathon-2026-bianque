"""Shared helpers: settings for a temporary lake and silver tables typed from the contracts."""

from datetime import date
from pathlib import Path

import duckdb
import pytest

from bianque.config import Settings
from bianque.pipeline.contracts import load_contracts
from bianque.pipeline.silver import LINEAGE, PARTITION_KEY, q, write_parquet

CONTRACTS = load_contracts(Path("contracts"))


def lake_settings(root: Path, **overrides) -> Settings:
    values = {
        "lake_root": Path(root),
        "contracts_dir": Path("contracts"),
        "silver_sql_dir": Path("sql/silver"),
        "late_arrival_days": 7,
        "age_reference_date": date(2026, 6, 17),
        "age_band_edges": (18, 25, 35, 45, 55, 65),
        "duckdb_memory_limit": "1GB",
        "duckdb_threads": 2,
    }
    return Settings(**{**values, **overrides})


def silver_columns(table: str) -> dict[str, str]:
    """Column -> type of a silver table, as silver writes it (PII bands, derived, lineage)."""
    c = CONTRACTS[table]
    cols = {n: col.type for n, col in c.columns.items() if n not in c.pii_age_band}
    cols |= {n: col.type for n, col in c.derived.items()}
    cols |= {"_source_key": "VARCHAR", "_source_etag": "VARCHAR", "_ingested_at": "TIMESTAMP"}
    return cols


def write_silver(root: Path, table: str, rows: list[dict]) -> None:
    """Write a silver table with the contract's schema; missing values are NULL."""
    cols = silver_columns(table)
    unknown = {k for r in rows for k in r} - set(cols)
    assert not unknown, f"{table}: unknown columns {unknown}"
    con = duckdb.connect()
    con.execute(f"CREATE TABLE t ({', '.join(f'{q(n)} {t}' for n, t in cols.items())})")
    names = list(cols)
    if rows:
        con.executemany(
            f"INSERT INTO t VALUES ({', '.join('?' for _ in names)})",
            [[r.get(n) for n in names] for r in rows],
        )
    pc = CONTRACTS[table].partition_column
    query = (
        f"SELECT *, strftime({q(pc)}, '%Y-%m') AS {PARTITION_KEY} FROM t"
        if pc
        else "SELECT * FROM t"
    )
    out = Path(root) / "silver" / table
    if rows:
        write_parquet(con, query, out, partitioned=pc is not None)
    else:
        # A partitioned COPY of zero rows writes nothing; keep one empty file with the schema.
        write_parquet(con, query, out, partitioned=False)


@pytest.fixture
def empty_silver(tmp_path):
    """A lake with every silver table present and empty."""
    for table in CONTRACTS:
        write_silver(tmp_path, table, [])
    return tmp_path


assert set(LINEAGE) <= set(silver_columns("transactions"))
