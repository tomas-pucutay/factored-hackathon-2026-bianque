"""Pipeline settings from configs/settings.yaml, with secrets and overrides from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml
from dotenv import load_dotenv

SETTINGS_PATH = Path("configs/settings.yaml")


@dataclass(frozen=True)
class Settings:
    lake_root: Path
    contracts_dir: Path
    silver_sql_dir: Path
    late_arrival_days: int
    age_reference_date: date
    age_band_edges: tuple[int, ...]
    duckdb_memory_limit: str
    duckdb_threads: int

    @property
    def bronze_root(self) -> Path:
        return self.lake_root / "bronze"

    @property
    def silver_root(self) -> Path:
        return self.lake_root / "silver"

    @property
    def meta_root(self) -> Path:
        return self.lake_root / "_meta"


def load_settings(path: Path = SETTINGS_PATH) -> Settings:
    load_dotenv()
    raw = yaml.safe_load(path.read_text())
    silver, duck = raw["silver"], raw["duckdb"]
    return Settings(
        lake_root=Path(os.getenv("LAKE_ROOT", raw["lake_root"])),
        contracts_dir=Path(raw["contracts_dir"]),
        silver_sql_dir=Path(raw["silver_sql_dir"]),
        late_arrival_days=int(silver["late_arrival_days"]),
        age_reference_date=date.fromisoformat(silver["age_reference_date"]),
        age_band_edges=tuple(silver["age_band_edges"]),
        duckdb_memory_limit=duck["memory_limit"],
        duckdb_threads=int(duck["threads"]),
    )


def pii_hash_key() -> str:
    """Secret key for PII tokenization. Required: without it tokens would be guessable."""
    load_dotenv()
    key = os.getenv("PII_HASH_KEY")
    if not key:
        raise SystemExit("PII_HASH_KEY is not set (see .env.example)")
    return key
