"""Silver layer: bronze Parquet -> typed, validated, deduplicated <LAKE_ROOT>/silver Parquet.

Every step is driven by the table's contract in contracts/<table>.yaml.
"""

from __future__ import annotations

import logging

import duckdb

from bianque.pipeline.contracts import Column, Contract

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
