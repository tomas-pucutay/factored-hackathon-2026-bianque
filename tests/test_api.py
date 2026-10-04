from datetime import date

import duckdb
from fastapi.testclient import TestClient

from bianque.api import main


def test_health_reports_the_serving_slice(tmp_path, monkeypatch):
    db = tmp_path / "serving.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute(
            "CREATE TABLE slice_info AS SELECT DATE '2026-06-17' AS as_of_date, "
            "'bayes_blocks_v1' AS model_version, 'cost_assumptions_v1' AS assumptions_version, "
            "300 AS n_customers"
        )
    monkeypatch.setattr(main, "SERVING_DB", db)

    response = TestClient(main.app).get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "serving_slice": {
            "as_of_date": date(2026, 6, 17).isoformat(),
            "model_version": "bayes_blocks_v1",
            "assumptions_version": "cost_assumptions_v1",
            "n_customers": 300,
        },
    }


def test_health_is_degraded_without_the_slice(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "SERVING_DB", tmp_path / "missing.duckdb")

    response = TestClient(main.app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "serving_slice": None}
