"""Bronze layer: S3 CSV -> <LAKE_ROOT>/bronze Parquet.

Incremental and idempotent:
  - Lists every object under the source prefix.
  - Compares them against the manifest (key + ETag).
  - Only processes new or modified objects (different ETag).
  - CSVs are streamed from S3 straight into Parquet; no raw copy is kept locally.
  - On failure, successful files stay in the manifest and failed ones are retried next run.

Usage:
  python -m bianque.pipeline.bronze --dry-run   # list what would be processed, no download
  python -m bianque.pipeline.bronze             # run the ingestion
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import boto3
import pandas as pd
from dotenv import load_dotenv
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

from bianque.pipeline.manifest import find_pending, load_manifest, manifest_path, save_manifest

log = logging.getLogger("bronze")

SOURCE_PREFIX = "data/"

# Facts (partitioned):              data/<dataset>/year=2026/month=06/day=17/<file>.csv
# Dimensions and references (root): data/<file>.csv -> dataset = file name, no partition
KEY_RE = re.compile(
    r"^data/(?:(?P<dataset>[^/=]+)/)?(?P<partition>(?:[^/]+=[^/]+/)*)(?P<filename>[^/]+)\.csv$"
)


@dataclass(frozen=True)
class Settings:
    bucket: str
    region: str
    lake_root: Path

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        bucket = os.getenv("AWS__BUCKET_NAME")
        if not bucket:
            raise SystemExit("AWS__BUCKET_NAME is not set (see .env.example)")
        return cls(
            bucket=bucket,
            region=os.getenv("AWS__REGION", "us-east-2"),
            lake_root=Path(os.getenv("LAKE_ROOT", "data")),
        )


def s3_client(region: str):
    # Explicit keys from .env when present; otherwise boto3's default credential chain.
    return boto3.client(
        "s3",
        aws_access_key_id=os.getenv("AWS__ACCESS_KEY_ID") or None,
        aws_secret_access_key=os.getenv("AWS__SECRET_ACCESS_KEY") or None,
        region_name=region,
    )


def list_source(s3, bucket: str) -> tuple[pd.DataFrame, list[str]]:
    """List every CSV under the prefix (paginated). Returns (csvs, skipped keys)."""
    rows, skipped = [], []
    paginator = s3.get_paginator("list_objects_v2")
    with tqdm(desc="Listing S3", unit=" obj") as bar:
        for page in paginator.paginate(Bucket=bucket, Prefix=SOURCE_PREFIX):
            contents = page.get("Contents", [])
            bar.update(len(contents))
            for obj in contents:
                m = KEY_RE.match(obj["Key"])
                if not m:
                    if not obj["Key"].endswith("/"):  # empty "folders" are noise
                        skipped.append(obj["Key"])
                    continue
                rows.append(
                    {
                        "key": obj["Key"],
                        "etag": obj["ETag"].strip('"'),
                        "size": obj["Size"],
                        "last_modified": obj["LastModified"],
                        "dataset": m["dataset"] or m["filename"],
                        "partition": m["partition"],
                        "filename": m["filename"],
                    }
                )
    return pd.DataFrame(rows), skipped


def bronze_path(lake_root: Path, item: dict) -> Path:
    """Mirror the source layout: bronze/<dataset>/<partition>/<file>.parquet."""
    return (
        lake_root / "bronze" / item["dataset"] / item["partition"] / f"{item['filename']}.parquet"
    )


def ingest_one(s3, bucket: str, lake_root: Path, item: dict) -> dict:
    """Read one CSV from S3 and write it as Parquet in bronze."""
    body = s3.get_object(Bucket=bucket, Key=item["key"])["Body"].read()

    # Bronze is a faithful copy: everything as text, only empty cells become null.
    # Types are fixed in silver.
    df = pd.read_csv(io.BytesIO(body), dtype="string", keep_default_na=False, na_values=[""])

    # Lineage: which file each row came from and when it was loaded
    df["_source_key"] = item["key"]
    df["_source_etag"] = item["etag"]
    df["_ingested_at"] = pd.Timestamp.now(tz="UTC")

    out = bronze_path(lake_root, item)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    df.to_parquet(tmp, engine="pyarrow", compression="zstd", index=False)
    tmp.replace(out)  # atomic write: never a half-written Parquet

    return {
        **item,
        "rows": len(df),
        "bronze_path": str(out),
        "ingested_at": datetime.now(UTC),
    }


def print_plan(pending: pd.DataFrame, skipped: list[str]) -> None:
    if not pending.empty:
        summary = pending.groupby("dataset").agg(files=("key", "size"), mb=("size", "sum"))
        summary["mb"] = (summary["mb"] / 1024**2).round(1)
        print(summary.to_string())
    if skipped:
        exts = Counter(Path(k).suffix or "<none>" for k in skipped)
        print(f"\nSkipped {len(skipped)} non-matching objects by extension: {dict(exts)}")
        for key in skipped[:10]:
            print(f"  {key}")


def ingest(
    s3,
    settings: Settings,
    pending: pd.DataFrame,
    max_workers: int,
    done: list[dict],
    failed: list[str],
) -> None:
    """Ingest pending objects in parallel with a byte-based progress bar.

    Results are appended to `done`/`failed` as they finish, so the caller keeps
    partial progress if the run is interrupted.
    """
    with (
        ThreadPoolExecutor(max_workers=max_workers) as pool,  # boto3 clients are thread-safe
        tqdm(
            total=int(pending["size"].sum()),
            desc="Bronze",
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
        ) as bar,
    ):
        futures = {
            pool.submit(ingest_one, s3, settings.bucket, settings.lake_root, item): item
            for item in pending.to_dict("records")
        }
        try:
            for fut in as_completed(futures):
                item = futures[fut]
                try:
                    done.append(fut.result())
                except Exception as exc:
                    failed.append(item["key"])
                    log.error("Failed %s: %s", item["key"], exc)
                bar.update(item["size"])
                bar.set_postfix(ok=len(done), failed=len(failed))
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            raise


def main(max_workers: int, dry_run: bool) -> None:
    settings = Settings.from_env()
    s3 = s3_client(settings.region)
    mpath = manifest_path(settings.lake_root)

    source, skipped = list_source(s3, settings.bucket)
    manifest = load_manifest(mpath)
    pending = find_pending(source, manifest)

    log.info(
        "In S3: %d CSVs (%d other objects) | in manifest: %d | pending: %d",
        len(source),
        len(skipped),
        len(manifest),
        len(pending),
    )
    if dry_run:
        print_plan(pending, skipped)
        return
    if pending.empty:
        return

    done: list[dict] = []
    failed: list[str] = []
    try:
        ingest(s3, settings, pending, max_workers, done, failed)
    finally:
        # Persist whatever succeeded, even on Ctrl+C, so the next run resumes.
        save_manifest(mpath, manifest, pd.DataFrame(done))

    log.info("Ingested: %d | Failed: %d", len(done), len(failed))
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="list pending files only")
    parser.add_argument("--workers", type=int, default=8, help="parallel downloads")
    args = parser.parse_args()
    with logging_redirect_tqdm():
        main(max_workers=args.workers, dry_run=args.dry_run)
