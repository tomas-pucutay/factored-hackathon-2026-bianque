"""Bianque API.

  make serve      # local, with reload
  make deploy     # Google Cloud Run (see README, Deployment)

The API never reads the lake: it reads the serving slice built by gold
(data/gold/serving/serving.duckdb, see bianque.pipeline.serving), shipped inside the image.
SERVING_DB overrides its path.
"""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
from fastapi import FastAPI

from bianque.api.conversations import router as conversations

SERVING_DB = Path(os.getenv("SERVING_DB", "data/gold/serving/serving.duckdb"))

app = FastAPI(
    title="Bianque",
    description=(
        "Proactive AI customer service for LATAM banking: "
        "the best complaint is the one that never arrives."
    ),
    version="0.1.0",
)
app.include_router(conversations)


def slice_info(path: Path) -> dict | None:
    """What the deployed slice contains, or None if it is missing."""
    if not path.exists():
        return None
    with duckdb.connect(str(path), read_only=True) as con:
        row = con.execute(
            "SELECT as_of_date, model_version, assumptions_version, n_customers FROM slice_info"
        ).fetchone()
    as_of, model_version, assumptions_version, n_customers = row
    return {
        "as_of_date": as_of.isoformat(),
        "model_version": model_version,
        "assumptions_version": assumptions_version,
        "n_customers": n_customers,
    }


@app.get("/health")
def health() -> dict:
    """Liveness plus what the service is running with. Degraded when the slice is missing."""
    info = slice_info(SERVING_DB)
    return {"status": "ok" if info else "degraded", "serving_slice": info}
