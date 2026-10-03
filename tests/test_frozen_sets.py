import json
from datetime import date

import duckdb
import pytest
from conftest import lake_settings

from bianque.evaluation.frozen_sets import FrozenSetChanged, freeze, sha256
from bianque.pipeline.silver import write_parquet

ROWS = [
    ("t_train", "2025-03-01", False),
    ("t_val1", "2025-08-01", True),
    ("t_val2", "2025-09-01", False),
    ("t_test", "2026-02-01", False),
    ("t_after", "2026-07-01", False),  # after test_end: in no set
]


def write_features(root, rows):
    values = ", ".join(f"('{t}', DATE '{d}', {str(f).upper()}, 1.0)" for t, d, f in rows)
    query = (
        "SELECT *, strftime(process_date, '%Y-%m') AS process_month FROM (VALUES "
        f"{values}) v(transaction_id, process_date, is_fraud, amount_usd)"
    )
    write_parquet(duckdb.connect(), query, root / "gold" / "transaction_features", True)


@pytest.fixture
def settings(tmp_path):
    write_features(tmp_path, ROWS)
    return lake_settings(
        tmp_path,
        eval_manifest=tmp_path / "eval" / "frozen" / "manifest.json",
        eval_train_end=date(2025, 7, 1),
        eval_validation_end=date(2026, 1, 1),
        eval_test_end=date(2026, 6, 18),
    )


def set_ids(path):
    return [r[0] for r in duckdb.sql(f"SELECT transaction_id FROM '{path}'").fetchall()]


def test_first_run_freezes_sets_and_records_hashes(settings):
    manifest = freeze(settings)
    frozen = settings.eval_manifest.parent

    assert json.loads(settings.eval_manifest.read_text()) == manifest
    assert set_ids(frozen / "fraud_validation.parquet") == ["t_val1", "t_val2"]
    assert set_ids(frozen / "fraud_test.parquet") == ["t_test"]
    val = manifest["sets"]["fraud_validation"]
    assert (val["rows"], val["fraud_rows"]) == (2, 1)
    assert val["sha256"] == sha256(frozen / "fraud_validation.parquet")
    assert "transaction_scores" not in str(manifest)  # no model output in eval sets


def test_rerun_and_fresh_clone_reproduce_the_same_hashes(settings):
    first = freeze(settings)
    assert freeze(settings) == first  # verified, manifest untouched

    for f in settings.eval_manifest.parent.glob("*.parquet"):
        f.unlink()  # a fresh clone has only the committed manifest
    assert freeze(settings) == first


def test_changed_data_fails_and_keeps_frozen_files(settings, tmp_path):
    first = freeze(settings)
    test_file = settings.eval_manifest.parent / "fraud_test.parquet"
    write_features(tmp_path, [*ROWS, ("t_late", "2026-03-01", True)])  # late test-period row

    with pytest.raises(FrozenSetChanged, match="fraud_test"):
        freeze(settings)
    assert sha256(test_file) == first["sets"]["fraud_test"]["sha256"]
    assert set_ids(test_file) == ["t_test"]

    refrozen = freeze(settings, refreeze=True)
    assert refrozen["sets"]["fraud_test"]["rows"] == 2
    assert refrozen["sets"]["fraud_validation"] == first["sets"]["fraud_validation"]
