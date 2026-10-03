"""Late-arrival watermark for silver facts: decide which months a run must reprocess.

State per table lives in <LAKE_ROOT>/_meta/silver_state/<table>.json:
  - watermark: highest process_date already in silver
  - fingerprints: size and mtime of every bronze file processed (new or rewritten = changed)
  - contract_hash: contract + table SQL; a change forces a full rebuild

Each incremental run reprocesses the months touched by changed bronze files plus the months
covering the trailing window [watermark - late_arrival_days, watermark]. Rows older than the
window are still applied (no data loss) but counted as late beyond the window.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import duckdb


@dataclass
class SilverState:
    watermark: date | None = None
    fingerprints: dict[str, str] = field(default_factory=dict)
    contract_hash: str = ""


@dataclass
class Plan:
    full: bool
    reason: str
    months: set[str] | None = None  # None means every month (full rebuild)
    late_rows: int = 0  # rows older than the trailing window
    watermark: date | None = None  # watermark after this run (None: read from output)


def state_path(meta_root: Path, table: str) -> Path:
    return meta_root / "silver_state" / f"{table}.json"


def load_state(path: Path) -> SilverState | None:
    if not path.exists():
        return None
    raw = json.loads(path.read_text())
    wm = raw.get("watermark")
    return SilverState(
        watermark=date.fromisoformat(wm) if wm else None,
        fingerprints=raw.get("fingerprints", {}),
        contract_hash=raw.get("contract_hash", ""),
    )


def save_state(path: Path, state: SilverState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(
            {
                "watermark": state.watermark.isoformat() if state.watermark else None,
                "fingerprints": state.fingerprints,
                "contract_hash": state.contract_hash,
            },
            indent=1,
            sort_keys=True,
        )
    )
    tmp.replace(path)


def fingerprint(path: str | Path) -> str:
    st = Path(path).stat()
    return f"{st.st_size}-{st.st_mtime_ns}"


def contract_hash(*files: Path) -> str:
    """Hash of the files that define a table's silver output (missing files are skipped)."""
    h = hashlib.sha256()
    for f in files:
        if f.exists():
            h.update(f.name.encode() + b"\0" + f.read_bytes())
    return h.hexdigest()


def month(d: date) -> str:
    return d.strftime("%Y-%m")


def window_months(watermark: date, days: int) -> set[str]:
    return {month(watermark - timedelta(days=i)) for i in range(days + 1)}


def plan(
    con: duckdb.DuckDBPyConnection,
    files: list[str],
    state: SilverState | None,
    current_hash: str,
    partition_column: str,
    late_arrival_days: int,
    full: bool = False,
) -> Plan:
    if full:
        return Plan(True, "requested")
    if state is None or state.watermark is None:
        return Plan(True, "no previous state")
    if state.contract_hash != current_hash:
        return Plan(True, "contract or table SQL changed")
    if set(state.fingerprints) - set(files):
        return Plan(True, "bronze files were removed")

    changed = [f for f in files if state.fingerprints.get(f) != fingerprint(f)]
    dates: dict[date, int] = {}
    if changed:
        dates = dict(
            con.execute(
                f"SELECT TRY_CAST({partition_column} AS DATE) AS d, count(*) "
                f"FROM read_parquet(?, union_by_name = true, hive_partitioning = false) "
                f"WHERE d IS NOT NULL GROUP BY d",
                [changed],
            ).fetchall()
        )
    watermark = max([state.watermark, *dates])
    window_start = watermark - timedelta(days=late_arrival_days)
    late = sum(n for d, n in dates.items() if d < window_start)
    months = {month(d) for d in dates} | window_months(watermark, late_arrival_days)
    return Plan(
        False,
        f"{len(changed)} changed file(s)",
        months=months,
        late_rows=late,
        watermark=watermark,
    )
