"""Gold layer: business-ready tables built from silver.

Each table is a portable SQL file in sql/gold/<table>.sql that reads silver tables by name
(and gold tables built before it). Outputs go to <LAKE_ROOT>/gold/<table>/.

Usage:
  python -m bianque.pipeline.gold
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import yaml

from bianque.config import Settings, load_settings
from bianque.models.baselines import build_transaction_scores
from bianque.pipeline.silver import connect, lit, q, write_parquet

log = logging.getLogger("gold")

# Build order -> partitioned by process_month (large facts) or a single file.
GOLD_TABLES: dict[str, bool] = {
    "transaction_features": True,
    "customer_360": False,
    "channel_costs": False,
    "service_cost_baseline": False,
    "dispute_outcomes": False,
    "agent_routing": False,
}


def relation(path: Path) -> str:
    return f"read_parquet({lit(str(path / '**' / '*.parquet'))}, hive_partitioning = true)"


def register_layer(con: duckdb.DuckDBPyConnection, root: Path) -> list[str]:
    """Expose every table folder under `root` as a view with the table's name."""
    names = []
    if root.exists():
        for p in sorted(root.iterdir()):
            if p.is_dir() and not p.name.startswith(("_", ".")) and "." not in p.name:
                con.execute(
                    f"CREATE OR REPLACE TEMP VIEW {q(p.name)} AS SELECT * FROM {relation(p)}"
                )
                names.append(p.name)
    return names


def register_cost_assumptions(con: duckdb.DuckDBPyConnection, path: Path) -> str:
    """Expose the synthetic cost assumptions as `contact_cost_assumptions` and
    `cost_assumptions` views. Returns the assumptions version."""
    raw = yaml.safe_load(path.read_text())
    version = raw["version"]
    rows = []
    for interaction_type, cost in raw["contact_costs"].items():
        if set(cost) not in ({"per_minute"}, {"per_contact"}):
            raise ValueError(
                f"{path}: {interaction_type} needs exactly one of per_minute / per_contact"
            )
        rows.append(
            f"({lit(interaction_type)}, {lit(cost.get('per_minute'))}::DOUBLE, "
            f"{lit(cost.get('per_contact'))}::DOUBLE)"
        )
    con.execute(
        "CREATE OR REPLACE TEMP VIEW contact_cost_assumptions AS "
        f"SELECT *, {lit(version)} AS assumptions_version FROM (VALUES {', '.join(rows)}) "
        "AS v(interaction_type, cost_per_minute_usd, cost_per_contact_usd)"
    )
    con.execute(
        "CREATE OR REPLACE TEMP VIEW cost_assumptions AS SELECT "
        f"{lit(version)} AS assumptions_version, "
        f"{float(raw['friction_cost_legit_usd'])}::DOUBLE AS friction_cost_legit_usd"
    )
    return version


def build_sql_table(
    con: duckdb.DuckDBPyConnection, settings: Settings, name: str, partitioned: bool
) -> int:
    """Run sql/gold/<name>.sql, write it to gold and register it as a view. Returns rows."""
    sql = (settings.gold_sql_dir / f"{name}.sql").read_text().strip().rstrip(";")
    out = settings.gold_root / name
    write_parquet(con, sql, out, partitioned)
    con.execute(f"CREATE OR REPLACE TEMP VIEW {q(name)} AS SELECT * FROM {relation(out)}")
    return con.execute(f"SELECT count(*) FROM {q(name)}").fetchone()[0]


def build(
    settings: Settings, tables: dict[str, bool] = GOLD_TABLES, scores: bool = True
) -> dict[str, int]:
    con = connect(settings)
    silver = register_layer(con, settings.silver_root)
    if not silver:
        raise FileNotFoundError(f"no silver tables under {settings.silver_root}: run silver first")
    register_cost_assumptions(con, settings.cost_assumptions)
    counts = {}
    for name, partitioned in tables.items():
        counts[name] = build_sql_table(con, settings, name, partitioned)
        log.info("%-22s rows=%10d", name, counts[name])
    if scores:
        counts["transaction_scores"] = build_transaction_scores(con, settings)
        log.info("%-22s rows=%10d", "transaction_scores", counts["transaction_scores"])
    return counts


def main() -> None:
    build(load_settings())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
