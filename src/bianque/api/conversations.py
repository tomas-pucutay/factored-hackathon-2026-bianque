"""Conversation endpoints: the agent behind HTTP.

Identity comes only from the `Authorization: Bearer <session token>` header of each request
(bianque.agent.session). A conversation belongs to the customer who started it: another
customer's token gets 404, as if it did not exist. Test sessions are issued by
POST /test/sessions, standing in for the bank's app login.

Inactivity: a conversation closes after IDLE_SECONDS without a request from its customer
(CONVERSATION_IDLE_SECONDS, default 180). Requests to a closed conversation get 410 Gone; a
keep-alive request resets the timer. Every turn returns the timeout so a client can warn the
customer before it closes.

Capacity: at most MAX_CONVERSATIONS conversations are kept in memory (503 past it). They live
in the instance's memory, so Cloud Run runs a single instance with session affinity
(scripts/deploy.sh).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from bianque.agent.graph import Agent
from bianque.agent.llm import GeminiExtractor, LLMUnavailable
from bianque.agent.session import SessionError, issue, verify
from bianque.agent.store import CaseStore
from bianque.agent.tools import Tools
from bianque.config import load_settings
from bianque.policy.engine import Charge, CustomerContext, decide, load_policy

MAX_CONVERSATIONS = 5_000
INBOX_SIZE = 30
IDLE_SECONDS = int(os.getenv("CONVERSATION_IDLE_SECONDS", "180"))


@dataclass
class Owner:
    customer_id: str
    last_seen: float
    closed: bool = False


router = APIRouter()
_owners: dict[str, Owner] = {}
_lock = threading.Lock()


@lru_cache(maxsize=1)
def get_agent() -> Agent:
    settings = load_settings()
    serving = Path(os.getenv("SERVING_DB", "data/gold/serving/serving.duckdb"))
    tools = Tools(
        serving_db=serving,
        store=CaseStore(),
        policy=load_policy(settings.contact_policy, settings.cost_assumptions),
    )
    try:
        extractor = GeminiExtractor()
    except LLMUnavailable:
        extractor = None  # the agent falls back to the deterministic menu
    return Agent(tools, extractor)


def _token(authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


def _owner_check(conversation_id: str, token: str | None) -> None:
    """404 for unknown or someone else's conversation, 410 once it closed for inactivity;
    otherwise the request counts as activity."""
    owner = _owners.get(conversation_id)
    if owner is None:
        raise HTTPException(404, "conversation not found")
    try:
        customer = verify(token).customer_id
    except SessionError:
        customer = None  # no valid session: the agent itself will ask the customer to log in
    if customer is not None and customer != owner.customer_id:
        raise HTTPException(404, "conversation not found")
    now = time.monotonic()
    if not owner.closed and now - owner.last_seen > IDLE_SECONDS:
        owner.closed = True
        get_agent().tools.store.audit(
            conversation_id, "closed_idle", {"idle_seconds": IDLE_SECONDS}
        )
    if owner.closed:
        raise HTTPException(410, f"conversation closed after {IDLE_SECONDS} s of inactivity")
    owner.last_seen = now


def _register(token: str | None) -> str:
    with _lock:
        if len(_owners) >= MAX_CONVERSATIONS:
            raise HTTPException(503, "capacity reached; try again later")
        try:
            customer = verify(token).customer_id
        except SessionError as e:
            raise HTTPException(401, str(e)) from e
        conversation_id = f"CONV-{uuid.uuid4().hex[:12].upper()}"
        _owners[conversation_id] = Owner(customer, time.monotonic())
    return conversation_id


class TurnOut(BaseModel):
    conversation_id: str
    reply: str
    stage: str
    language: str
    actions: list[dict]
    handoff: dict | None
    decision: dict | None
    understood_by: str = Field(description="gemini, menu (fallback) or none")
    latency_ms: float
    idle_timeout_seconds: int = Field(description="the conversation closes after this long idle")


def _out(conversation_id: str, state: dict, started: float) -> TurnOut:
    return TurnOut(
        conversation_id=conversation_id,
        reply=state.get("reply", ""),
        stage=state.get("stage", ""),
        language=state.get("language", "es"),
        actions=state.get("actions", []),
        handoff=state.get("handoff"),
        decision=state.get("decision"),
        understood_by=state.get("llm_mode", "none"),
        latency_ms=round((time.monotonic() - started) * 1000, 1),
        idle_timeout_seconds=IDLE_SECONDS,
    )


class SessionIn(BaseModel):
    customer_id: str
    ttl_seconds: int = Field(900, ge=1, le=3600)


class ProactiveIn(BaseModel):
    transaction_id: str
    language: Literal["es", "pt"] = "es"


class MessageIn(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


@router.post("/test/sessions", tags=["test"])
def create_test_session(body: SessionIn) -> dict:
    """TEST ONLY: a trusted session for a customer of the serving slice (simulates the app
    login). In production this is the bank's identity provider."""
    tools = get_agent().tools
    if not tools._q("SELECT 1 FROM customers WHERE customer_id = ?", [body.customer_id]):
        raise HTTPException(404, "customer not in the serving slice")
    try:
        token = issue(body.customer_id, body.ttl_seconds)
    except SessionError as e:
        raise HTTPException(503, str(e)) from e
    return {"token": token, "expires_in_seconds": body.ttl_seconds}


@router.post("/conversations/proactive", tags=["conversations"])
def start_proactive(body: ProactiveIn, authorization: str | None = Header(None)) -> TurnOut:
    """Bianque writes first: the contact policy decides whether to alert about a charge."""
    started, token = time.monotonic(), _token(authorization)
    conversation_id = _register(token)
    state = get_agent().start_proactive(conversation_id, token, body.transaction_id, body.language)
    return _out(conversation_id, state, started)


@router.post("/conversations/reactive", tags=["conversations"])
def start_reactive(body: MessageIn, authorization: str | None = Header(None)) -> TurnOut:
    """The customer writes first about a charge they do not recognize."""
    started, token = time.monotonic(), _token(authorization)
    conversation_id = _register(token)
    state = get_agent().start_reactive(conversation_id, token, body.message)
    return _out(conversation_id, state, started)


@router.post("/conversations/{conversation_id}/messages", tags=["conversations"])
def send_message(
    conversation_id: str, body: MessageIn, authorization: str | None = Header(None)
) -> TurnOut:
    started, token = time.monotonic(), _token(authorization)
    _owner_check(conversation_id, token)
    state = get_agent().reply(conversation_id, token, body.message)
    return _out(conversation_id, state, started)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _stream(conversation_id: str, token: str | None, message: str, reactive: bool):
    """Server-sent events: one `step` per graph node as it finishes, the reply in `reply`
    chunks, then `done` with the full turn (same body as the non-streaming endpoint)."""
    started = time.monotonic()
    try:
        for kind, value in get_agent().stream_reply(conversation_id, token, message, reactive):
            if kind == "step":
                yield _sse("step", {"node": value})
            else:
                for chunk in re.findall(r"\S+\s*", value.get("reply", "")):
                    yield _sse("reply", {"text": chunk})
                yield _sse("done", _out(conversation_id, value, started).model_dump())
    except Exception as e:  # the stream has started: report the error as an event
        yield _sse("error", {"detail": f"{type(e).__name__}: {e}"})


def _sse_response(events) -> StreamingResponse:
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/conversations/reactive/stream", tags=["conversations"])
def start_reactive_stream(body: MessageIn, authorization: str | None = Header(None)):
    """Streaming version of POST /conversations/reactive (server-sent events)."""
    token = _token(authorization)
    conversation_id = _register(token)
    return _sse_response(_stream(conversation_id, token, body.message, reactive=True))


@router.post("/conversations/{conversation_id}/messages/stream", tags=["conversations"])
def send_message_stream(
    conversation_id: str, body: MessageIn, authorization: str | None = Header(None)
):
    """Streaming version of POST /conversations/{id}/messages (server-sent events)."""
    token = _token(authorization)
    _owner_check(conversation_id, token)
    return _sse_response(_stream(conversation_id, token, body.message, reactive=False))


@router.post("/conversations/{conversation_id}/keepalive", tags=["conversations"])
def keepalive(conversation_id: str, authorization: str | None = Header(None)) -> dict:
    """The customer is still there: reset the inactivity timer."""
    _owner_check(conversation_id, _token(authorization))
    return {"conversation_id": conversation_id, "idle_timeout_seconds": IDLE_SECONDS}


@router.get("/conversations/{conversation_id}", tags=["conversations"])
def get_conversation(conversation_id: str, authorization: str | None = Header(None)) -> dict:
    """The conversation, its verified actions, handoff package and audit log."""
    token = _token(authorization)
    try:
        verify(token)
    except SessionError as e:
        raise HTTPException(401, str(e)) from e
    _owner_check(conversation_id, token)
    agent = get_agent()
    state = agent.state(conversation_id)
    return {
        "conversation_id": conversation_id,
        "stage": state.get("stage"),
        "language": state.get("language"),
        "transcript": state.get("transcript", []),
        "actions": state.get("actions", []),
        "handoff": state.get("handoff"),
        "decision": state.get("decision"),
        "audit": agent.tools.store.audit_log(conversation_id),
    }


@router.get("/demo/inbox", tags=["demo"])
def demo_inbox() -> list[dict]:
    """DEMO ONLY: recent charges of the serving slice with the contact policy's decision, for
    the demo page. Customer IDs are the slice's synthetic identifiers; no personal data."""
    agent = get_agent()
    tools = agent.tools
    rows = tools._q(
        f"""SELECT t.customer_id, t.transaction_id, t.transaction_date, t.amount_usd,
                   t.merchant_name, t.p_fraud, t.p_fraud_low, t.p_fraud_high, p.product_type,
                   c.main_digital_channel, c.n_complaints_365d, c.country, s.reason
            FROM transactions t
            JOIN products p ON p.product_id = t.product_id
            JOIN customers c ON c.customer_id = t.customer_id
            JOIN slice_customers s ON s.customer_id = t.customer_id
            WHERE s.reason = 'proactive_target' AND t.p_fraud >= 0.5
               OR s.reason = 'recent_dispute' AND t.amount_usd >= 1000
            ORDER BY t.p_fraud DESC, t.transaction_date DESC
            LIMIT {INBOX_SIZE}"""
    )
    channels = tools.channels()
    out = []
    for r in rows:
        d = decide(
            tools.policy,
            Charge(
                r["transaction_id"],
                float(r["amount_usd"]),
                float(r["p_fraud"]),
                r["p_fraud_low"],
                r["p_fraud_high"],
            ),
            CustomerContext(r["main_digital_channel"], int(r["n_complaints_365d"] or 0)),
            channels,
        )
        out.append(
            {
                "customer_id": r["customer_id"],
                "transaction_id": r["transaction_id"],
                "date": r["transaction_date"].isoformat(sep=" ", timespec="minutes"),
                "amount_usd": float(r["amount_usd"]),
                "merchant": r["merchant_name"],
                "product_type": r["product_type"],
                "country": r["country"],
                "p_fraud": float(r["p_fraud"]),
                "decision": {
                    "action": d.action,
                    "channel": d.channel,
                    "handled_by": d.handled_by,
                    "reasons": [{"rule": x.rule, "detail": x.detail} for x in d.reasons],
                    "policy_version": d.policy_version,
                },
            }
        )
    return out
