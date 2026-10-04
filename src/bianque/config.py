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
    gold_sql_dir: Path = Path("sql/gold")
    eval_manifest: Path = Path("eval/frozen/manifest.json")
    cost_assumptions: Path = Path("policies/cost_assumptions_v1.yaml")
    contact_policy: Path = Path("policies/contact_policy_v1.yaml")
    fraud_model: Path | None = None  # trained calibrator; None scores with the baseline
    eval_train_end: date = date(2025, 7, 1)
    eval_validation_end: date = date(2026, 1, 1)
    eval_test_end: date = date(2026, 6, 18)
    serving_customers: int = 300

    @property
    def bronze_root(self) -> Path:
        return self.lake_root / "bronze"

    @property
    def silver_root(self) -> Path:
        return self.lake_root / "silver"

    @property
    def gold_root(self) -> Path:
        return self.lake_root / "gold"

    @property
    def meta_root(self) -> Path:
        return self.lake_root / "_meta"


def load_settings(path: Path = SETTINGS_PATH) -> Settings:
    load_dotenv()
    raw = yaml.safe_load(path.read_text())
    silver, gold, duck = raw["silver"], raw["gold"], raw["duckdb"]
    return Settings(
        lake_root=Path(os.getenv("LAKE_ROOT", raw["lake_root"])),
        contracts_dir=Path(raw["contracts_dir"]),
        silver_sql_dir=Path(raw["silver_sql_dir"]),
        late_arrival_days=int(silver["late_arrival_days"]),
        age_reference_date=date.fromisoformat(silver["age_reference_date"]),
        age_band_edges=tuple(silver["age_band_edges"]),
        duckdb_memory_limit=duck["memory_limit"],
        duckdb_threads=int(duck["threads"]),
        gold_sql_dir=Path(raw["gold_sql_dir"]),
        eval_manifest=Path(raw["eval_manifest"]),
        cost_assumptions=Path(raw["cost_assumptions"]),
        contact_policy=Path(raw["contact_policy"]),
        fraud_model=Path(raw["fraud_model"]),
        eval_train_end=date.fromisoformat(gold["train_end"]),
        eval_validation_end=date.fromisoformat(gold["validation_end"]),
        eval_test_end=date.fromisoformat(gold["test_end"]),
        serving_customers=int(gold["serving_customers"]),
    )


def pii_hash_key() -> str:
    """Secret key for PII tokenization. Required: without it tokens would be guessable."""
    load_dotenv()
    key = os.getenv("PII_HASH_KEY")
    if not key:
        raise SystemExit("PII_HASH_KEY is not set (see .env.example)")
    return key
