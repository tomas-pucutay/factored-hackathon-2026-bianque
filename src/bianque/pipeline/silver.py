"""Silver layer: bronze Parquet -> typed, validated, deduplicated <LAKE_ROOT>/silver Parquet.

Every step is driven by the table's contract in contracts/<table>.yaml.

Usage:
  python -m bianque.pipeline.silver                     # all tables, parents first
  python -m bianque.pipeline.silver --table customers   # one or more tables
"""

from __future__ import annotations

import argparse
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from bianque.config import Settings, load_settings
from bianque.pipeline.contracts import Column, Contract, build_order, load_contracts

log = logging.getLogger("silver")

LINEAGE = ("_source_key", "_source_etag", "_ingested_at")


class SchemaError(Exception):
    """Breaking schema change: a contract column is missing from the data."""


def file_columns(con: duckdb.DuckDBPyConnection, files: list[str]) -> dict[str, set[str]]:
    """Column names of each Parquet file, read from metadata only."""
    rows = con.execute(
        "SELECT file_name, list(name) FROM parquet_schema(?) "
        "WHERE num_children IS NULL GROUP BY file_name",
        [files],
    ).fetchall()
    return {f: set(cols) for f, cols in rows}


def compare_schema(contract: Contract, columns_by_file: dict[str, set[str]]) -> list[str]:
    """Raise on missing contract columns; return additive columns, sorted."""
    expected = set(contract.columns)
    broken = {f: expected - cols for f, cols in columns_by_file.items() if expected - cols}
    if broken:
        sample = "\n  ".join(f"{f}: missing {sorted(m)}" for f, m in list(broken.items())[:5])
        raise SchemaError(f"{contract.table}: {len(broken)} file(s) break the contract\n  {sample}")
    seen = set().union(*columns_by_file.values()) if columns_by_file else set()
    extra = sorted(seen - expected - set(LINEAGE))
    if extra:
        log.warning(
            "%s: additive columns not in the contract, kept as text: %s", contract.table, extra
        )
    return extra


def q(name: str) -> str:
    """Quote an identifier."""
    return '"' + name.replace('"', '""') + '"'


def lit(value: object) -> str:
    """SQL literal for a contract value."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list | tuple):
        return "[" + ", ".join(lit(v) for v in value) + "]"
    return "'" + str(value).replace("'", "''") + "'"


def _mapped(expr: str, mapping: dict[str, str | None]) -> str:
    if not mapping:
        return expr
    whens = " ".join(f"WHEN {lit(k)} THEN {lit(v)}" for k, v in mapping.items())
    return f"CASE {expr} {whens} ELSE {expr} END"


def _cast(expr: str, base_type: str) -> str:
    """Cast text to the contract type; NULL when the value does not fit."""
    if base_type == "VARCHAR":
        return expr
    if base_type == "INTEGER":
        # CSVs carry integers as floats ("701.0"); reject real decimals instead of rounding.
        d = f"TRY_CAST({expr} AS DOUBLE)"
        return f"CASE WHEN {d} = trunc({d}) THEN TRY_CAST({d} AS INTEGER) END"
    return f"TRY_CAST({expr} AS {base_type})"


def typed_column(col: Column, mapping: dict[str, str | None]) -> tuple[str, str]:
    """(typed expression, source expression after value_map) for one column."""
    raw = q(col.name)
    if col.is_list:
        element = _cast(_mapped("x", mapping), col.base_type)
        source = f"string_split({raw}, {lit(col.split)})"
        return f"list_transform({source}, x -> {element})", raw
    source = _mapped(raw, mapping)
    return _cast(source, col.base_type), source


def error_checks(col: Column, typed: str, source: str) -> list[str]:
    """SQL expressions that yield an error label or NULL for one column."""
    checks = []
    if col.is_list:
        bad = f"list_count({typed}) < len(string_split({source}, {lit(col.split)}))"
        checks.append(f"CASE WHEN {source} IS NOT NULL AND {bad} THEN 'cast:{col.name}' END")
    elif col.base_type != "VARCHAR":
        checks.append(
            f"CASE WHEN {source} IS NOT NULL AND {typed} IS NULL THEN 'cast:{col.name}' END"
        )
    if not col.nullable:
        checks.append(f"CASE WHEN {typed} IS NULL THEN 'null:{col.name}' END")
    return checks


def typed_select(contract: Contract, source: str, extra_columns: list[str] = ()) -> str:
    """SELECT that types every contract column and collects row errors in `_errors`.

    `extra_columns` are columns found in the data but not in the contract (additive schema
    changes): they pass through as text.
    """
    selects, checks = [], []
    for col in contract.columns.values():
        typed, src = typed_column(col, contract.value_map.get(col.name, {}))
        selects.append(f"{typed} AS {q(col.name)}")
        checks += error_checks(col, typed, src)
    selects += [q(c) for c in extra_columns]
    selects += [q(c) for c in LINEAGE]
    errors = f"list_filter([{', '.join(checks)}]::VARCHAR[], e -> e IS NOT NULL)"
    return f"SELECT {', '.join(selects)}, {errors} AS _errors FROM {source}"


def dedupe_select(contract: Contract, source: str) -> str:
    """Keep one row per primary key: the first by the contract's dedupe_order."""
    pk = ", ".join(q(c) for c in contract.primary_key)
    order = ", ".join(contract.dedupe_order)
    return (
        f"SELECT * EXCLUDE (_rn) FROM (SELECT *, row_number() OVER "
        f"(PARTITION BY {pk} ORDER BY {order}) AS _rn FROM {source}) WHERE _rn = 1"
    )


def fk_select(contract: Contract, source: str, parents: dict[str, str]) -> str:
    """Check foreign keys against the parents' silver tables.

    Adds `_orphans` (FKs with on_orphan: quarantine) and `_nullified` (FKs with
    on_orphan: nullify, whose value is replaced by NULL). `parents` maps table -> relation.
    """
    joins, orphans, nullified, replaces = [], [], [], []
    for i, fk in enumerate(contract.foreign_keys):
        p, col = f"_p{i}", f"s.{q(fk.column)}"
        joins.append(
            f"LEFT JOIN (SELECT DISTINCT {q(fk.ref_column)} AS k FROM {parents[fk.table]}) {p} "
            f"ON {col} = {p}.k"
        )
        is_orphan = f"{col} IS NOT NULL AND {p}.k IS NULL"
        if fk.on_orphan == "nullify":
            nullified.append(f"CASE WHEN {is_orphan} THEN '{fk.column}' END")
            replaces.append(f"CASE WHEN {is_orphan} THEN NULL ELSE {col} END AS {q(fk.column)}")
        else:
            orphans.append(f"CASE WHEN {is_orphan} THEN 'orphan:{fk.column}' END")
    star = f"s.* REPLACE ({', '.join(replaces)})" if replaces else "s.*"

    def labels(items: list[str]) -> str:
        return f"list_filter([{', '.join(items)}]::VARCHAR[], e -> e IS NOT NULL)"

    return (
        f"SELECT {star}, {labels(orphans)} AS _orphans, {labels(nullified)} AS _nullified "
        f"FROM {source} s {' '.join(joins)}"
    )


@dataclass
class TableResult:
    table: str
    rows_in: int = 0
    quarantined: int = 0
    duplicates: int = 0
    rows_out: int = 0
    nullified: dict[str, int] = field(default_factory=dict)


def connect(settings: Settings) -> duckdb.DuckDBPyConnection:
    tmp = settings.lake_root / "_tmp" / "duckdb"
    tmp.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"SET memory_limit = {lit(settings.duckdb_memory_limit)}")
    con.execute(f"SET threads = {settings.duckdb_threads}")
    con.execute(f"SET temp_directory = {lit(str(tmp))}")
    con.execute("SET preserve_insertion_order = false")
    return con


def silver_relation(settings: Settings, table: str) -> str:
    path = settings.silver_root / table
    if not path.exists():
        raise FileNotFoundError(f"silver table {table!r} not built yet: build its parents first")
    return f"read_parquet({lit(str(path / '**' / '*.parquet'))}, hive_partitioning = true)"


def bronze_files(settings: Settings, table: str) -> list[str]:
    return sorted(str(p) for p in (settings.bronze_root / table).rglob("*.parquet"))


PARTITION_KEY = "process_month"


def partitioned_select(query: str, partition_column: str) -> str:
    """Add the physical partition key: one folder per month of the partition column.

    Daily folders would mean ~1,100 tiny files per table; a month is small enough to
    rewrite when late arrivals touch it.
    """
    return f"SELECT *, strftime({q(partition_column)}, '%Y-%m') AS {PARTITION_KEY} FROM ({query})"


def write_parquet(
    con: duckdb.DuckDBPyConnection, query: str, out: Path, partition_column: str | None
) -> None:
    """Write a query to a Parquet folder, replacing the old one only after success.

    With a partition column the output is Hive-partitioned by month (`process_month=YYYY-MM`).
    """
    tmp = out.with_name(out.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    if partition_column:
        opts = f"FORMAT parquet, COMPRESSION zstd, PARTITION_BY ({PARTITION_KEY})"
        con.execute(
            f"COPY ({partitioned_select(query, partition_column)}) TO {lit(str(tmp))} ({opts})"
        )
    else:
        tmp.mkdir()
        con.execute(
            f"COPY ({query}) TO {lit(str(tmp / 'data.parquet'))} (FORMAT parquet, COMPRESSION zstd)"
        )
    old = out.with_name(out.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if out.exists():
        out.rename(old)
    tmp.rename(out)
    shutil.rmtree(old, ignore_errors=True)


def apply_table_sql(
    con: duckdb.DuckDBPyConnection, settings: Settings, contract: Contract, source: str, out: str
) -> None:
    """Create view `out`: `source` transformed by sql/silver/<table>.sql when that file exists.

    The SQL reads the rows from `input` and may join any silver table built before by its
    name (e.g. daily_exchange_rates). It must return the input columns plus the contract's
    derived columns.
    """
    path = settings.silver_sql_dir / f"{contract.table}.sql"
    if not path.exists():
        con.execute(f"CREATE OR REPLACE TEMP VIEW {out} AS SELECT * FROM {source}")
        return
    built = settings.silver_root.iterdir() if settings.silver_root.exists() else []
    for parent in sorted(p.name for p in built if p.is_dir()):
        if parent != contract.table and not parent.startswith(("_", ".")) and "." not in parent:
            con.execute(
                f"CREATE OR REPLACE TEMP VIEW {q(parent)} AS SELECT * FROM {silver_relation(settings, parent)}"
            )
    sql = path.read_text().strip().rstrip(";")
    con.execute(
        f"CREATE OR REPLACE TEMP VIEW {out} AS WITH input AS (SELECT * FROM {source}) {sql}"
    )
    got = [r[0] for r in con.execute(f"DESCRIBE {out}").fetchall()]
    expected = [r[0] for r in con.execute(f"DESCRIBE {source}").fetchall()] + list(contract.derived)
    if sorted(got) != sorted(expected):
        raise ValueError(
            f"{path}: output columns differ from input + derived: "
            f"missing {sorted(set(expected) - set(got))}, unexpected {sorted(set(got) - set(expected))}"
        )


def build_table(
    con: duckdb.DuckDBPyConnection, settings: Settings, contract: Contract
) -> TableResult:
    t = contract.table
    result = TableResult(t)
    files = bronze_files(settings, t)
    if not files:
        raise FileNotFoundError(f"{t}: no bronze files under {settings.bronze_root / t}")
    extra = compare_schema(contract, file_columns(con, files))
    source = f"read_parquet({lit(files)}, union_by_name = true, hive_partitioning = false)"

    con.execute(f"CREATE OR REPLACE TEMP TABLE staged AS {typed_select(contract, source, extra)}")
    result.rows_in = con.execute("SELECT count(*) FROM staged").fetchone()[0]

    # Rows that cannot be typed or miss a required value: kept aside with the reason.
    con.execute(
        "CREATE OR REPLACE TEMP TABLE quarantine AS SELECT * EXCLUDE (_errors), "
        "array_to_string(_errors, '; ') AS _reason FROM staged WHERE len(_errors) > 0"
    )
    con.execute(
        "CREATE OR REPLACE TEMP VIEW valid AS "
        "SELECT * EXCLUDE (_errors) FROM staged WHERE len(_errors) = 0"
    )
    con.execute(f"CREATE OR REPLACE TEMP VIEW deduped AS {dedupe_select(contract, 'valid')}")

    # Foreign keys against the parents' silver tables (built first by build_order).
    parents = {fk.table: silver_relation(settings, fk.table) for fk in contract.foreign_keys}
    kind = "TABLE" if contract.foreign_keys else "VIEW"
    con.execute(
        f"CREATE OR REPLACE TEMP {kind} checked AS {fk_select(contract, 'deduped', parents)}"
    )
    con.execute(
        "INSERT INTO quarantine SELECT * EXCLUDE (_orphans, _nullified), "
        "array_to_string(_orphans, '; ') FROM checked WHERE len(_orphans) > 0"
    )
    result.nullified = dict(
        con.execute(
            "SELECT e, count(*) FROM (SELECT unnest(_nullified) AS e FROM checked) GROUP BY e"
        ).fetchall()
    )
    con.execute(
        "CREATE OR REPLACE TEMP VIEW input AS "
        "SELECT * EXCLUDE (_orphans, _nullified) FROM checked WHERE len(_orphans) = 0"
    )
    apply_table_sql(con, settings, contract, "input", "final")

    result.quarantined = con.execute("SELECT count(*) FROM quarantine").fetchone()[0]
    quarantine_dir = settings.silver_root / "_quarantine" / t
    if result.quarantined:
        write_parquet(con, "SELECT * FROM quarantine", quarantine_dir, None)
    else:
        shutil.rmtree(quarantine_dir, ignore_errors=True)

    out = settings.silver_root / t
    write_parquet(con, "SELECT * FROM final", out, contract.partition_column)
    # Count from the written files (Parquet metadata) instead of re-running the query.
    result.rows_out = con.execute(
        f"SELECT count(*) FROM read_parquet({lit(str(out / '**' / '*.parquet'))})"
    ).fetchone()[0]
    result.duplicates = result.rows_in - result.quarantined - result.rows_out
    # Free the temp objects, dependents first.
    for name, obj in [
        ("final", "VIEW"),
        ("input", "VIEW"),
        ("checked", kind),
        ("deduped", "VIEW"),
        ("valid", "VIEW"),
        ("quarantine", "TABLE"),
        ("staged", "TABLE"),
    ]:
        con.execute(f"DROP {obj} IF EXISTS {name}")
    return result


def main(tables: list[str] | None = None) -> list[TableResult]:
    settings = load_settings()
    contracts = load_contracts(settings.contracts_dir)
    order = [t for t in build_order(contracts) if not tables or t in tables]
    unknown = set(tables or []) - set(contracts)
    if unknown:
        raise SystemExit(f"Unknown tables: {sorted(unknown)}")
    con = connect(settings)
    results = []
    for t in order:
        r = build_table(con, settings, contracts[t])
        log.info(
            "%-26s in=%10d quarantined=%8d duplicates=%8d out=%10d nullified=%s",
            t,
            r.rows_in,
            r.quarantined,
            r.duplicates,
            r.rows_out,
            r.nullified or "-",
        )
        results.append(r)
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--table", action="append", help="build only this table (repeatable)")
    args = parser.parse_args()
    main(args.table)
