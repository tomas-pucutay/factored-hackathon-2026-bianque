"""Tools: the only way the agent reads customer data or acts. Permissions live here.

Every customer-facing tool takes a verified Session and enforces, in code:
  - the session is valid and not expired (bianque.agent.session);
  - the resource belongs to the session's customer: another customer's charge, case or block
    does not exist for this session (PermissionDenied, without revealing whether it exists);
  - the policy's requirements for the action (policies/contact_policy_v1.yaml, actions),
    from facts the agent verified, e.g. {"authenticated_session", "intent_not_mine",
    "explicit_confirmation"} (RequirementsNotMet).
The language model never calls these tools and never sees their raw results: the graph
calls them and gives the model only the facts it needs to phrase a reply.

Reads come from the serving slice (read-only DuckDB); writes go to the CaseStore. A tool
named in `fail` raises ToolFailure, to evaluate tool failures and the bounded retries.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb

from bianque.agent.session import Session, SessionError
from bianque.agent.store import CaseStore
from bianque.policy.engine import (
    ChannelStats,
    Charge,
    CustomerContext,
    Decision,
    Policy,
    decide,
    missing_requirements,
)

RECENT_DAYS = 90
AMOUNT_TOLERANCE = 0.01  # a mentioned amount matches within 1%


class PermissionDenied(Exception):
    """The resource does not belong to the session's customer."""


class RequirementsNotMet(Exception):
    """The policy requires facts the agent has not verified."""


class ToolFailure(Exception):
    """A tool could not complete (to retry, then hand off)."""


@dataclass(frozen=True)
class ChargeView:
    """What the agent may know about a charge: no customer or product identifiers."""

    transaction_id: str
    transaction_date: str
    amount_usd: float
    merchant: str | None
    product_type: str
    p_fraud: float
    p_fraud_low: float | None
    p_fraud_high: float | None


@dataclass
class Tools:
    serving_db: Path
    store: CaseStore
    policy: Policy
    fail: frozenset[str] = frozenset()
    _con: duckdb.DuckDBPyConnection = field(init=False, repr=False)
    _lock: threading.Lock = field(init=False, repr=False, default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self._con = duckdb.connect(str(self.serving_db), read_only=True)

    def _q(self, sql: str, params: list | None = None) -> list[dict]:
        with self._lock:
            cur = self._con.execute(sql, params or [])
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]

    def _guard(self, name: str, session: Session) -> None:
        if name in self.fail:
            raise ToolFailure(f"{name} is unavailable")
        if session.expired():
            raise SessionError("session expired: the customer must log in again")

    def _owned_charge(self, session: Session, transaction_id: str) -> dict:
        rows = self._q(
            """SELECT t.transaction_id, t.transaction_date, t.amount_usd, t.merchant_name,
                      t.product_id, p.product_type, t.p_fraud, t.p_fraud_low, t.p_fraud_high
               FROM transactions t JOIN products p USING (product_id)
               WHERE t.transaction_id = ? AND t.customer_id = ?""",
            [transaction_id, session.customer_id],
        )
        if not rows:
            raise PermissionDenied("charge not found for this customer")
        return rows[0]

    @staticmethod
    def _view(row: dict) -> ChargeView:
        return ChargeView(
            transaction_id=row["transaction_id"],
            transaction_date=row["transaction_date"].isoformat(sep=" ", timespec="minutes"),
            amount_usd=float(row["amount_usd"]),
            merchant=row["merchant_name"],
            product_type=row["product_type"],
            p_fraud=float(row["p_fraud"]),
            p_fraud_low=None if row["p_fraud_low"] is None else float(row["p_fraud_low"]),
            p_fraud_high=None if row["p_fraud_high"] is None else float(row["p_fraud_high"]),
        )

    def _require(self, action: str, facts: set[str]) -> None:
        missing = missing_requirements(self.policy, action, facts)
        if missing:
            raise RequirementsNotMet(f"{action} requires {missing}")

    # --- reads -------------------------------------------------------------------------

    def get_charge(self, session: Session, transaction_id: str) -> ChargeView:
        self._guard("get_charge", session)
        return self._view(self._owned_charge(session, transaction_id))

    def find_charges(
        self,
        session: Session,
        amount_usd: float | None = None,
        on_date: date | None = None,
        merchant: str | None = None,
    ) -> list[ChargeView]:
        """The customer's recent charges matching what they described, newest first."""
        self._guard("find_charges", session)
        rows = self._q(
            f"""SELECT t.transaction_id, t.transaction_date, t.amount_usd, t.merchant_name,
                       t.product_id, p.product_type, t.p_fraud, t.p_fraud_low, t.p_fraud_high
                FROM transactions t JOIN products p USING (product_id)
                WHERE t.customer_id = ?
                  AND t.transaction_date >= (SELECT max(transaction_date) FROM transactions)
                                            - INTERVAL {RECENT_DAYS} DAYS
                ORDER BY t.transaction_date DESC""",
            [session.customer_id],
        )
        out = []
        for r in rows:
            amount = float(r["amount_usd"])
            if amount_usd is not None and abs(amount - amount_usd) > AMOUNT_TOLERANCE * max(
                amount_usd, 1.0
            ):
                continue
            if on_date is not None and abs((r["transaction_date"].date() - on_date).days) > 1:
                continue
            if merchant and merchant.lower() not in (r["merchant_name"] or "").lower():
                continue
            out.append(self._view(r))
        return out

    def customer_context(self, session: Session) -> tuple[CustomerContext, str]:
        """Policy context and country of the session's customer (never their identity)."""
        self._guard("customer_context", session)
        rows = self._q(
            "SELECT main_digital_channel, n_complaints_365d, country FROM customers "
            "WHERE customer_id = ?",
            [session.customer_id],
        )
        if not rows:
            raise PermissionDenied("unknown customer")
        r = rows[0]
        return CustomerContext(r["main_digital_channel"], int(r["n_complaints_365d"] or 0)), r[
            "country"
        ]

    def channels(self) -> dict[str, ChannelStats]:
        rows = self._q("SELECT channel, cost_per_delivered, open_rate FROM channel_costs")
        return {
            r["channel"]: ChannelStats(
                r["channel"],
                float(r["cost_per_delivered"]),
                None if r["open_rate"] is None else float(r["open_rate"]),
            )
            for r in rows
        }

    def policy_decision(self, session: Session, transaction_id: str) -> Decision:
        """The contact policy applied to one of the customer's charges."""
        charge = self.get_charge(session, transaction_id)
        context, _ = self.customer_context(session)
        return decide(
            self.policy,
            Charge(
                charge.transaction_id,
                charge.amount_usd,
                charge.p_fraud,
                charge.p_fraud_low,
                charge.p_fraud_high,
            ),
            context,
            self.channels(),
        )

    # --- actions (each checks the policy's requirements) --------------------------------

    def open_dispute(self, session: Session, transaction_id: str, facts: set[str]) -> dict:
        self._guard("open_dispute", session)
        self._require("open_dispute", facts)
        self._owned_charge(session, transaction_id)
        existing = self.store.open_case_for(transaction_id)
        if existing and existing["customer_id"] == session.customer_id:
            return existing  # idempotent: one open dispute per charge
        case_id = self.store.add_case(session.customer_id, transaction_id, "dispute")
        return self.store.get_case(case_id)

    def block_product(
        self, session: Session, transaction_id: str, case_id: str, facts: set[str]
    ) -> dict:
        """Provisional block of the product the charge was made with."""
        self._guard("block_product", session)
        self._require("provisional_card_block", facts)
        charge = self._owned_charge(session, transaction_id)
        case = self.store.get_case(case_id)
        if not case or case["customer_id"] != session.customer_id:
            raise PermissionDenied("case not found for this customer")
        block_id = self.store.add_block(session.customer_id, charge["product_id"], case_id)
        return self.store.get_block(block_id)

    def close_as_legitimate(self, session: Session, transaction_id: str, facts: set[str]) -> dict:
        """The customer recognizes the charge: store it as a future label."""
        self._guard("close_as_legitimate", session)
        self._require("close_as_legitimate", facts)
        self._owned_charge(session, transaction_id)
        self.store.add_label(session.customer_id, transaction_id, "legitimate", "customer")
        return self.store.get_label(transaction_id)

    # --- verification: read back what an action wrote ------------------------------------

    def verify_case(self, session: Session, case_id: str) -> dict:
        self._guard("verify_case", session)
        case = self.store.get_case(case_id)
        if not case or case["customer_id"] != session.customer_id:
            raise PermissionDenied("case not found for this customer")
        return case

    def verify_block(self, session: Session, block_id: str) -> dict:
        self._guard("verify_block", session)
        block = self.store.get_block(block_id)
        if not block or block["customer_id"] != session.customer_id:
            raise PermissionDenied("block not found for this customer")
        return block

    # --- handoff routing (internal, no customer data) ------------------------------------

    def route_agent(self, language: str, fraud: bool = True) -> dict | None:
        """Best available human agent who speaks the language: fraud specialists first, then
        measured first-contact resolution."""
        if language not in ("es", "pt", "en"):
            language = "es"
        rows = self._q(
            f"""SELECT agent_id, is_fraud_specialist, first_contact_resolution_rate_90d AS fcr
                FROM agent_routing
                WHERE is_available AND speaks_{language}
                ORDER BY is_fraud_specialist = ? DESC, fcr DESC NULLS LAST, agent_id
                LIMIT 1""",
            [fraud],
        )
        return rows[0] if rows else None
