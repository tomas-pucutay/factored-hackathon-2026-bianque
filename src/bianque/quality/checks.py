"""Data quality checks on silver and gold, generated from the contracts (make quality).

Every rule a contract declares becomes a check on the silver table, with its denominator:

  completeness  required columns (nullable: false) have no NULLs; missing-data rate of every
                optional column, outside its structural NULLs (reported, never filled)
  uniqueness    one row per primary key
  validity      allowed values (per element for list columns) and ranges
  consistency   structural NULLs (null_when): a value where the field does not apply; the
                business-day rule behind process_date (process_day); a few business rules
  integrity     foreign keys resolve in the parent table (orphans were quarantined or nullified)
  lineage       bronze rows (manifest) = silver rows + quarantined rows + removed duplicates;
                gold row counts match silver
  timeliness    freshness of each fact table and days without data (update policy)

Severity: error = a contract rule is broken; warn = worth a look; info = measurement only.
Results go to data/_meta/quality/results.json; bianque.quality.report writes the report.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from bianque.config import Settings, load_settings
from bianque.pipeline.contracts import Column, Contract, load_contracts
from bianque.pipeline.gold import register_layer

log = logging.getLogger("bianque.quality")

MAX_EXAMPLES = 3


@dataclass
class Check:
    table: str
    dimension: (
        str  # completeness, uniqueness, validity, consistency, integrity, lineage, timeliness
    )
    name: str
    severity: str  # error, warn, info
    failing: int
    total: int
    column: str | None = None
    detail: str = ""
    examples: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.severity == "info" or self.failing == 0

    @property
    def rate(self) -> float:
        return self.failing / self.total if self.total else 0.0


def qi(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sql_value(v: object) -> str:
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int | float):
        return str(v)
    return "'" + str(v).replace("'", "''") + "'"


def condition_sql(cond: dict) -> str:
    """One null_when condition: every column matches one of its values (None matches NULL)."""
    parts = []
    for col, values in cond.items():
        concrete = [v for v in values if v is not None]
        options = []
        if concrete:
            options.append(f"{qi(col)} IN ({', '.join(sql_value(v) for v in concrete)})")
        if None in values:
            options.append(f"{qi(col)} IS NULL")
        parts.append("(" + " OR ".join(options) + ")")
    return "(" + " AND ".join(parts) + ")"


def column_expressions(col: Column) -> dict[str, tuple[str, str, str, str]]:
    """key -> (dimension, check name, severity, failing-row predicate) for one column."""
    c = qi(col.name)
    out = {}
    if not col.nullable:
        out[f"notnull:{col.name}"] = (
            "completeness",
            "required value present",
            "error",
            f"{c} IS NULL",
        )
    if col.allowed is not None:
        values = ", ".join(sql_value(v) for v in col.allowed)
        pred = (
            f"len(list_filter({c}, x -> x NOT IN ({values}))) > 0"
            if col.is_list
            else f"{c} IS NOT NULL AND {c} NOT IN ({values})"
        )
        out[f"allowed:{col.name}"] = ("validity", "allowed values", "error", pred)
    if col.range is not None:
        lo, hi = col.range
        bounds = []
        if lo is not None:
            bounds.append(f"{c} < {lo}")
        if hi is not None:
            bounds.append(f"{c} > {hi}")
        out[f"range:{col.name}"] = ("validity", f"range [{lo}, {hi}]", "error", " OR ".join(bounds))
    if col.null_when:
        applies_not = " OR ".join(condition_sql(cond) for cond in col.null_when)
        out[f"null_when:{col.name}"] = (
            "consistency",
            "structural NULL (value where the field does not apply)",
            "error",
            f"{c} IS NOT NULL AND ({applies_not})",
        )
        out[f"missing:{col.name}"] = (
            "completeness",
            "missing where the field applies",
            "info",
            f"{c} IS NULL AND NOT ({applies_not})",
        )
        out[f"applies:{col.name}"] = ("_", "", "", f"NOT ({applies_not})")  # denominator
    elif col.nullable:
        out[f"missing:{col.name}"] = (
            "completeness",
            "missing (optional column)",
            "info",
            f"{c} IS NULL",
        )
    return out


def process_day_predicate(contract: Contract) -> str | None:
    pd = contract.process_day
    if pd is None or pd.timestamp is None:
        return None
    hours, minutes = (int(x) for x in pd.cutoff.split(":"))
    ts, pdate = qi(pd.timestamp), qi(contract.partition_column or "process_date")
    expected = f"CAST({ts} - INTERVAL {hours * 60 + minutes} MINUTE AS DATE)"
    late = f"CAST({ts} - INTERVAL {hours * 60 + minutes + pd.tolerance_minutes} MINUTE AS DATE)"
    # At exactly the cutoff second the source assigns either day (about half each): its
    # timestamps are truncated to the second. Those rows are counted apart (cutoff_tie).
    return (
        f"{ts} IS NOT NULL AND {pdate} <> {expected} AND {pdate} <> {late} "
        f"AND NOT ({cutoff_tie_predicate(contract)})"
    )


def cutoff_tie_predicate(contract: Contract) -> str:
    pd = contract.process_day
    return f"strftime({qi(pd.timestamp)}, '%H:%M:%S') = '{pd.cutoff}:00'"


def examples(con, table: str, contract: Contract, predicate: str) -> list[str]:
    key = ", ".join(qi(k) for k in contract.primary_key)
    rows = con.execute(
        f"SELECT {key} FROM {qi(table)} WHERE {predicate} ORDER BY {key} LIMIT {MAX_EXAMPLES}"
    ).fetchall()
    return [" / ".join(str(v) for v in r) for r in rows]


def table_checks(con, contract: Contract) -> list[Check]:
    t = contract.table
    exprs: dict[str, tuple[str, str, str, str]] = {}
    for col in [*contract.columns.values(), *contract.derived.values()]:
        if col.name in contract.pii_age_band:
            continue  # dropped in silver (replaced by age_band)
        exprs.update(column_expressions(col))
    pd_pred = process_day_predicate(contract)
    if pd_pred:
        exprs["process_day"] = (
            "consistency",
            "process_date follows the business-day rule",
            "error",
            pd_pred,
        )
        exprs["cutoff_tie"] = (
            "consistency",
            "events at exactly the cutoff second (either day accepted)",
            "info",
            cutoff_tie_predicate(contract),
        )
    key = ", ".join(qi(k) for k in contract.primary_key)
    select = ", ".join(
        ["count(*) AS n", f"count(*) - count(DISTINCT ({key})) AS dup"]
        + [f"count(*) FILTER (WHERE {pred}) AS {qi(k)}" for k, (*_, pred) in exprs.items()]
    )  # fmt: skip
    row = con.execute(f"SELECT {select} FROM {qi(t)}").fetchone()
    names = ["n", "dup", *exprs]
    values = dict(zip(names, row, strict=True))
    n = values["n"]
    checks = [
        Check(t, "uniqueness", f"one row per primary key ({', '.join(contract.primary_key)})",
              "error", values["dup"], n),
    ]  # fmt: skip
    for k, (dimension, name, severity, pred) in exprs.items():
        if dimension == "_":
            continue
        column = k.split(":", 1)[1] if ":" in k else None
        total = values.get(f"applies:{column}", n) if k.startswith("missing:") else n
        failing = values[k]
        ex = examples(con, t, contract, pred) if failing and severity != "info" else []
        checks.append(Check(t, dimension, name, severity, failing, total, column, examples=ex))
    pd = contract.process_day
    if pd and pd.inherits_table:
        via = qi(pd.inherits_via)
        failing = con.execute(
            f"""SELECT count(*) FROM {qi(t)} c JOIN {qi(pd.inherits_table)} p USING ({via})
                WHERE c.process_date <> p.process_date"""
        ).fetchone()[0]
        checks.append(
            Check(t, "consistency", f"process_date equals the {pd.inherits_table} row's", "error",
                  failing, n, "process_date")
        )  # fmt: skip
    for fk in contract.foreign_keys:
        orphans = con.execute(
            f"""SELECT count(*) FROM {qi(t)} c
                WHERE c.{qi(fk.column)} IS NOT NULL
                  AND NOT EXISTS (SELECT 1 FROM {qi(fk.table)} p
                                  WHERE p.{qi(fk.ref_column)} = c.{qi(fk.column)})"""
        ).fetchone()[0]
        checks.append(
            Check(t, "integrity", f"foreign key to {fk.table}.{fk.ref_column}", "error", orphans, n,
                  fk.column, detail=f"on_orphan: {fk.on_orphan}")
        )  # fmt: skip
    return checks


def bronze_rows(settings: Settings) -> dict[str, int]:
    manifest = settings.meta_root / "manifest.parquet"
    rows = duckdb.sql(f"SELECT dataset, sum(rows) FROM read_parquet('{manifest}') GROUP BY 1")
    return {d: int(r) for d, r in rows.fetchall()}


def count_files(path: Path) -> int:
    if not path.exists() or not any(path.rglob("*.parquet")):
        return 0
    return duckdb.sql(
        f"SELECT count(*) FROM read_parquet('{path}/**/*.parquet', hive_partitioning = true)"
    ).fetchone()[0]


def lineage_checks(con, contracts: dict[str, Contract], settings: Settings) -> list[Check]:
    bronze = bronze_rows(settings)
    checks = []
    for t in contracts:
        silver = con.execute(f"SELECT count(*) FROM {qi(t)}").fetchone()[0]
        quarantined = count_files(settings.silver_root / "_quarantine" / t)
        removed = bronze.get(t, 0) - silver - quarantined
        checks.append(
            Check(t, "lineage", "bronze rows = silver + quarantined + removed duplicates", "error",
                  int(removed < 0), bronze.get(t, 0),
                  detail=f"bronze {bronze.get(t, 0):,}; silver {silver:,}; quarantined "
                         f"{quarantined:,}; duplicates removed {removed:,}")
        )  # fmt: skip
        checks.append(
            Check(t, "lineage", "rows in quarantine", "warn", quarantined, bronze.get(t, 0))
        )
    gold = settings.gold_root
    tx = con.execute("SELECT count(*) FROM transactions").fetchone()[0]
    for name in ("transaction_features", "transaction_scores"):
        n = count_files(gold / name)
        checks.append(
            Check(name, "lineage", "one gold row per silver transaction", "error", abs(n - tx), tx,
                  detail=f"gold {n:,}; silver {tx:,}")
        )  # fmt: skip
    customers = con.execute("SELECT count(*) FROM customers").fetchone()[0]
    n = count_files(gold / "customer_360")
    checks.append(
        Check("customer_360", "lineage", "one gold row per silver customer", "error",
              abs(n - customers), customers, detail=f"gold {n:,}; silver {customers:,}")
    )  # fmt: skip
    return checks


def timeliness_checks(con, contracts: dict[str, Contract], settings: Settings) -> list[Check]:
    """Freshness: each fact table's last process day vs the newest across all of them, and
    process days without data inside each table's range."""
    facts = [c for c in contracts.values() if c.kind == "fact"]
    spans = {
        c.table: con.execute(
            f"SELECT min(process_date), max(process_date), count(DISTINCT process_date) "
            f"FROM {qi(c.table)}"
        ).fetchone()
        for c in facts
    }
    newest = max(s[1] for s in spans.values())
    checks = []
    for table, (first, last, days) in spans.items():
        lag = (newest - last).days
        expected_days = (last - first).days + 1
        checks.append(
            Check(table, "timeliness", f"freshness: last process day within {settings.late_arrival_days} days of the newest",
                  "warn", int(lag > settings.late_arrival_days), 1,
                  detail=f"last {last}; newest {newest}; lag {lag} days")
        )  # fmt: skip
        checks.append(
            Check(table, "timeliness", "process days without data", "warn",
                  expected_days - days, expected_days, detail=f"{first} to {last}")
        )  # fmt: skip
    return checks


def business_checks(con) -> list[Check]:
    """Rules from the data findings (docs/silver_data_findings.md) that no contract encodes."""
    out = []
    n = con.execute("SELECT count(*) FROM transactions").fetchone()[0]
    missing = con.execute("SELECT count(*) FROM transactions WHERE amount_usd IS NULL").fetchone()[
        0
    ]
    out.append(Check("transactions", "completeness", "amount_usd filled for every transaction",
                     "error", missing, n, "amount_usd"))  # fmt: skip
    n, late = con.execute(
        """SELECT count(*), count(*) FILTER (WHERE s.send_date::DATE > m.end_date)
           FROM campaign_sends s JOIN marketing_campaigns m USING (campaign_id)"""
    ).fetchone()
    out.append(Check("campaign_sends", "consistency", "send within its campaign's dates", "warn",
                     late, n, detail="known finding: sends after the campaign's end_date"))  # fmt: skip
    n, before = con.execute(
        """SELECT count(*), count(*) FILTER (WHERE resolution_date < creation_date)
           FROM complaints WHERE resolution_date IS NOT NULL"""
    ).fetchone()
    out.append(Check("complaints", "consistency", "resolved after it was created", "warn", before, n,
                     "resolution_date"))  # fmt: skip
    return out


def update_policy(settings: Settings) -> dict:
    """What the update and freshness policy did in the last runs (manifest and watermarks)."""
    manifest = settings.meta_root / "manifest.parquet"
    last_ingest = duckdb.sql(
        f"SELECT max(ingested_at)::VARCHAR FROM read_parquet('{manifest}')"
    ).fetchone()[0]
    states = {}
    for f in sorted((settings.meta_root / "silver_state").glob("*.json")):
        state = json.loads(f.read_text())
        states[f.stem] = {
            "watermark": state.get("watermark"),
            "bronze_files": len(state.get("fingerprints", {})),
        }
    return {
        "last_bronze_ingestion": str(last_ingest),
        "late_arrival_days": settings.late_arrival_days,
        "silver_watermarks": states,
    }


def run(settings: Settings) -> dict:
    start = time.monotonic()
    contracts = load_contracts(settings.contracts_dir)
    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{settings.duckdb_memory_limit}'")
    con.execute(f"SET threads = {settings.duckdb_threads}")
    register_layer(con, settings.silver_root)
    checks: list[Check] = []
    for table, contract in contracts.items():
        t0 = time.monotonic()
        checks += table_checks(con, contract)
        log.info("%-26s %3d checks  %5.1f s", table, len(checks), time.monotonic() - t0)
    checks += lineage_checks(con, contracts, settings)
    checks += timeliness_checks(con, contracts, settings)
    checks += business_checks(con)
    return {
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "seconds": round(time.monotonic() - start, 1),
        "tables": len(contracts),
        "update_policy": update_policy(settings),
        "checks": [asdict(c) | {"passed": c.passed, "rate": c.rate} for c in checks],
    }


def results_path(settings: Settings) -> Path:
    return settings.meta_root / "quality" / "results.json"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = load_settings()
    results = run(settings)
    out = results_path(settings)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, default=str) + "\n")
    failed = [c for c in results["checks"] if not c["passed"] and c["severity"] == "error"]
    log.info("%d checks, %d errors; wrote %s", len(results["checks"]), len(failed), out)


if __name__ == "__main__":
    main()
