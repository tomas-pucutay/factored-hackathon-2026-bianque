"""Ingestion manifest: which S3 objects (key + ETag) are already in bronze.

The manifest makes bronze incremental and idempotent: an object is processed only
if its key is new or its ETag changed since the last successful ingestion.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def manifest_path(lake_root: Path) -> Path:
    return lake_root / "_meta" / "manifest.parquet"


def load_manifest(path: Path) -> pd.DataFrame:
    if path.exists():
        return pd.read_parquet(path)
    return pd.DataFrame(columns=["key", "etag"])


def find_pending(source: pd.DataFrame, manifest: pd.DataFrame) -> pd.DataFrame:
    """New objects (not in the manifest) or modified ones (different ETag)."""
    if source.empty or manifest.empty:
        return source
    merged = source.merge(manifest[["key", "etag"]], on="key", how="left", suffixes=("", "_done"))
    return merged[merged["etag_done"] != merged["etag"]].drop(columns="etag_done")


def save_manifest(path: Path, old: pd.DataFrame, new: pd.DataFrame) -> None:
    """Upsert `new` rows by key and write atomically."""
    if new.empty:
        return
    combined = (
        new if old.empty else pd.concat([old[~old["key"].isin(new["key"])], new], ignore_index=True)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    combined.to_parquet(tmp, index=False)
    tmp.replace(path)
