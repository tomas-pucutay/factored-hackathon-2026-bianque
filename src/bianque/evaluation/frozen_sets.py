"""Frozen evaluation sets: fixed out-of-time slices of gold.transaction_features, with hashes.

Evaluation artifacts live in eval/, not in gold, so the leakage controls are easy to see:
  - Split on process_date: train < train_end <= validation < validation_end <= test < test_end.
    Train is not frozen here: models read it from gold and calibrate on it.
  - No model scores in the sets, so they do not depend on any model version.
  - Each set is written deterministically (sorted, single thread, fixed row groups) and its
    SHA-256 is recorded in eval/frozen/manifest.json, which is committed. The Parquet files
    are git-ignored.

On every run the sets are rebuilt in a temporary folder and compared with the manifest:
  - no manifest: freeze (write the files and the manifest);
  - same hashes: nothing changes;
  - a different hash: fail and keep the frozen files, unless refreeze=True.
A fresh clone without the files rebuilds them and must reproduce the committed hashes.
Hashes depend on the DuckDB version pinned in uv.lock.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from bianque.config import Settings
from bianque.pipeline.silver import lit

ROW_GROUP_SIZE = 100_000


class FrozenSetChanged(Exception):
    """A frozen evaluation set no longer matches its recorded hash."""


def split_bounds(settings: Settings) -> dict[str, tuple[str, str]]:
    """Set name -> [from, to) on process_date."""
    return {
        "fraud_validation": (
            settings.eval_train_end.isoformat(),
            settings.eval_validation_end.isoformat(),
        ),
        "fraud_test": (
            settings.eval_validation_end.isoformat(),
            settings.eval_test_end.isoformat(),
        ),
    }


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_set(
    con: duckdb.DuckDBPyConnection, features: str, bounds: tuple[str, str], out: Path
) -> dict:
    """Write one set deterministically and describe it."""
    start, end = bounds
    query = (
        f"SELECT * EXCLUDE (process_month) FROM {features} "
        f"WHERE process_date >= DATE {lit(start)} AND process_date < DATE {lit(end)} "
        "ORDER BY transaction_id"
    )
    threads = con.execute("SELECT current_setting('threads')").fetchone()[0]
    con.execute("SET threads = 1")
    try:
        con.execute(
            f"COPY ({query}) TO {lit(str(out))} "
            f"(FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {ROW_GROUP_SIZE})"
        )
    finally:
        con.execute(f"SET threads = {threads}")
    rows, frauds = con.execute(
        f"SELECT count(*), count(*) FILTER (WHERE is_fraud) FROM read_parquet({lit(str(out))})"
    ).fetchone()
    return {
        "process_date_from": start,
        "process_date_to_exclusive": end,
        "rows": rows,
        "fraud_rows": frauds,
        "sha256": sha256(out),
    }


def freeze(settings: Settings, refreeze: bool = False) -> dict:
    """Build, verify or (re)freeze the evaluation sets. Returns the manifest."""
    manifest_path = settings.eval_manifest
    frozen_dir = manifest_path.parent
    features = (
        "read_parquet("
        f"{lit(str(settings.gold_root / 'transaction_features' / '**' / '*.parquet'))}, "
        "hive_partitioning = true)"
    )
    tmp = frozen_dir / ".building"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    con = duckdb.connect()
    built = {
        name: write_set(con, features, bounds, tmp / f"{name}.parquet")
        for name, bounds in split_bounds(settings).items()
    }

    old = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    if old and not refreeze:
        changed = {
            name: (old["sets"].get(name, {}).get("sha256"), info["sha256"])
            for name, info in built.items()
            if old["sets"].get(name, {}).get("sha256") != info["sha256"]
        }
        if changed:
            shutil.rmtree(tmp)
            details = "; ".join(f"{n}: {a} -> {b}" for n, (a, b) in changed.items())
            raise FrozenSetChanged(
                f"frozen evaluation sets changed ({details}). If the change is intended, "
                "rerun with --refreeze and commit the new manifest."
            )

    for name in built:
        (tmp / f"{name}.parquet").replace(frozen_dir / f"{name}.parquet")
    shutil.rmtree(tmp)
    if old and not refreeze:
        return old  # verified: same content, manifest untouched
    manifest = {
        "frozen_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": "gold.transaction_features",
        "split_on": "process_date",
        "train_end_exclusive": settings.eval_train_end.isoformat(),
        "sets": {name: {"file": f"{name}.parquet", **info} for name, info in built.items()},
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest
