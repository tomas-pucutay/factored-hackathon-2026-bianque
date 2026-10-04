"""Case store: what the agent's actions write, and the audit log.

The serving slice is read-only; everything an action creates (disputes, provisional blocks,
"it's mine" labels, handoffs) and every audit event goes here, in SQLite.

Data retention: on Cloud Run the file lives in the container's temporary disk (CASES_DB,
default /tmp), so cases and audit events last while the instance runs and are gone when it
scales to zero. That is the intended behavior of the demo; a production deployment would
point this at a managed database with a retention policy.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    case_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, transaction_id TEXT NOT NULL,
    case_type TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS blocks (
    block_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, product_id TEXT NOT NULL,
    case_id TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS labels (
    transaction_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, label TEXT NOT NULL,
    source TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handoffs (
    handoff_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, agent_id TEXT,
    package TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL, at TEXT NOT NULL,
    step TEXT NOT NULL, detail TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


class CaseStore:
    def __init__(self, path: str | Path | None = None) -> None:
        path = path or os.getenv("CASES_DB", "/tmp/bianque_cases.sqlite")
        self._con = sqlite3.connect(str(path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._con.executescript(SCHEMA)

    def _write(self, sql: str, params: tuple) -> None:
        with self._lock, self._con:
            self._con.execute(sql, params)

    def _read(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._con.execute(sql, params).fetchall()]

    def add_case(self, customer_id: str, transaction_id: str, case_type: str) -> str:
        case_id = new_id("CASE")
        self._write(
            "INSERT INTO cases VALUES (?, ?, ?, ?, 'open', ?)",
            (case_id, customer_id, transaction_id, case_type, now()),
        )
        return case_id

    def get_case(self, case_id: str) -> dict | None:
        rows = self._read("SELECT * FROM cases WHERE case_id = ?", (case_id,))
        return rows[0] if rows else None

    def open_case_for(self, transaction_id: str) -> dict | None:
        rows = self._read(
            "SELECT * FROM cases WHERE transaction_id = ? AND status = 'open'", (transaction_id,)
        )
        return rows[0] if rows else None

    def add_block(self, customer_id: str, product_id: str, case_id: str) -> str:
        block_id = new_id("BLK")
        self._write(
            "INSERT INTO blocks VALUES (?, ?, ?, ?, ?)",
            (block_id, customer_id, product_id, case_id, now()),
        )
        return block_id

    def get_block(self, block_id: str) -> dict | None:
        rows = self._read("SELECT * FROM blocks WHERE block_id = ?", (block_id,))
        return rows[0] if rows else None

    def add_label(self, customer_id: str, transaction_id: str, label: str, source: str) -> None:
        self._write(
            "INSERT OR REPLACE INTO labels VALUES (?, ?, ?, ?, ?)",
            (transaction_id, customer_id, label, source, now()),
        )

    def get_label(self, transaction_id: str) -> dict | None:
        rows = self._read("SELECT * FROM labels WHERE transaction_id = ?", (transaction_id,))
        return rows[0] if rows else None

    def add_handoff(self, conversation_id: str, agent_id: str | None, package: dict) -> str:
        handoff_id = new_id("HND")
        self._write(
            "INSERT INTO handoffs VALUES (?, ?, ?, ?, ?)",
            (handoff_id, conversation_id, agent_id, json.dumps(package, default=str), now()),
        )
        return handoff_id

    def audit(self, conversation_id: str, step: str, detail: dict) -> None:
        self._write(
            "INSERT INTO audit (conversation_id, at, step, detail) VALUES (?, ?, ?, ?)",
            (conversation_id, now(), step, json.dumps(detail, default=str)),
        )

    def audit_log(self, conversation_id: str) -> list[dict]:
        rows = self._read(
            "SELECT at, step, detail FROM audit WHERE conversation_id = ? ORDER BY seq",
            (conversation_id,),
        )
        return [{**r, "detail": json.loads(r["detail"])} for r in rows]
