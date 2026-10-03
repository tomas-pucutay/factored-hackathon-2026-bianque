"""Silver updates correctly with the team-generated late-arrival fixture.

See fixtures/README.md for what the fixture contains and the expected result.
"""

from datetime import date
from pathlib import Path

import pytest

from bianque.config import Settings
from bianque.pipeline.bronze import KEY_RE, ingest_one
from bianque.pipeline.contracts import load_contracts
from bianque.pipeline.silver import connect, run_table

FIXTURE = Path("fixtures/late_arrival_v2")
CONTRACTS = load_contracts(Path("contracts"))
PII_KEY = "fixture-key"


class LocalS3:
    """Serves fixture files as if they were S3 objects."""

    def __init__(self, root: Path):
        self.root = root

    def get_object(self, Bucket, Key):
        return {"Body": (self.root / Key).open("rb")}


def ingest_stage(stage: str, lake: Path) -> None:
    """Bronze-ingest every CSV of a fixture stage, exactly like the real pipeline."""
    root = FIXTURE / stage
    for path in sorted(root.rglob("*.csv")):
        key = path.relative_to(root).as_posix()
        m = KEY_RE.match(key)
        item = {
            "key": key,
            "etag": f"{stage}-{path.name}",
            "dataset": m["dataset"] or m["filename"],
            "partition": m["partition"],
            "filename": m["filename"],
        }
        ingest_one(LocalS3(root), "fixture", lake, item)


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        lake_root=tmp_path,
        contracts_dir=Path("contracts"),
        silver_sql_dir=Path("sql/silver"),
        late_arrival_days=7,
        age_reference_date=date(2026, 6, 17),
        age_band_edges=(18, 25, 35, 45, 55, 65),
        duckdb_memory_limit="1GB",
        duckdb_threads=2,
    )
    return settings, connect(settings)


def build(settings, con, tables):
    return {t: run_table(con, settings, CONTRACTS[t], PII_KEY) for t in tables}


def transactions(con, lake):
    return con.sql(
        "SELECT transaction_id, amount, currency, amount_usd, amount_usd_source, process_month "
        f"FROM read_parquet('{lake}/silver/transactions/**/*.parquet', hive_partitioning = true,"
        " union_by_name = true) ORDER BY transaction_id"
    ).fetchall()


def test_late_partition_duplicate_and_new_column_update_silver(env):
    settings, con = env
    lake = settings.lake_root

    ingest_stage("base", lake)
    build(settings, con, ["branches", "customers", "products", "transactions"])
    assert [r[0] for r in transactions(con, lake)] == ["TRX-FIX-0001", "TRX-FIX-0002"]

    ingest_stage("late", lake)
    result = build(settings, con, ["transactions"])["transactions"]

    # Incremental: only the late month and the trailing window are rewritten.
    assert result.mode == "incremental ['2024-01', '2026-06']"
    assert result.late_rows == 2
    assert result.duplicates == 1
    assert result.quarantined == 0

    rows = transactions(con, lake)
    assert [(r[0], str(r[1]), r[2], str(r[3]), r[4], r[5]) for r in rows] == [
        ("TRX-FIX-0001", "400000.00", "COP", "100.00", "source", "2026-06"),
        ("TRX-FIX-0002", "7000.00", "ARS", "20.00", "source", "2026-06"),  # duplicate: latest wins
        (
            "TRX-FIX-0003",
            "800000.00",
            "COP",
            "200.00",
            "booking_rate",
            "2024-01",
        ),  # 800,000 / 4,000
        ("TRX-FIX-0004", "7000.00", "ARS", "20.00", "source", "2024-01"),
    ]

    # Additive column: kept as text where it exists, NULL elsewhere.
    fingerprints = con.sql(
        "SELECT transaction_id, device_fingerprint FROM read_parquet("
        f"'{lake}/silver/transactions/**/*.parquet', union_by_name = true) ORDER BY 1"
    ).fetchall()
    assert fingerprints == [
        ("TRX-FIX-0001", None),
        ("TRX-FIX-0002", None),
        ("TRX-FIX-0003", "fp-fixture-a"),
        ("TRX-FIX-0004", "fp-fixture-b"),
    ]


def test_rerun_without_changes_is_idempotent(env):
    settings, con = env
    lake = settings.lake_root
    ingest_stage("base", lake)
    ingest_stage("late", lake)
    build(settings, con, ["branches", "customers", "products", "transactions"])
    before = transactions(con, lake)

    again = build(settings, con, ["transactions"])["transactions"]

    assert again.mode == "incremental ['2026-06']"  # only the trailing window
    assert again.late_rows == 0
    assert transactions(con, lake) == before
