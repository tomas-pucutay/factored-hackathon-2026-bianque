"""Conversation endpoints: the agent behind HTTP.

Identity comes only from the `Authorization: Bearer <session token>` header of each request
(bianque.agent.session). A conversation belongs to the customer who started it: another
customer's token gets 404, as if it did not exist. Test sessions are issued by
POST /test/sessions, standing in for the bank's app login.

Capacity: at most MAX_CONVERSATIONS conversations are kept in memory per instance (503 past
it); Cloud Run runs at most 2 instances (scripts/deploy.sh).
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from bianque.agent.graph import Agent
from bianque.agent.llm import GeminiExtractor, LLMUnavailable
from bianque.agent.session import SessionError, issue, verify
from bianque.agent.store import CaseStore
from bianque.agent.tools import Tools
from bianque.config import load_settings
from bianque.policy.engine import load_policy

MAX_CONVERSATIONS = 5_000

router = APIRouter()
_owners: dict[str, str] = {}
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
    owner = _owners.get(conversation_id)
    if owner is None:
        raise HTTPException(404, "conversation not found")
    try:
        customer = verify(token).customer_id
    except SessionError:
        return  # no valid session: the agent itself will ask the customer to log in
    if customer != owner:
        raise HTTPException(404, "conversation not found")


def _register(token: str | None) -> str:
    with _lock:
        if len(_owners) >= MAX_CONVERSATIONS:
            raise HTTPException(503, "capacity reached; try again later")
        try:
            customer = verify(token).customer_id
        except SessionError as e:
            raise HTTPException(401, str(e)) from e
        conversation_id = f"CONV-{uuid.uuid4().hex[:12].upper()}"
        _owners[conversation_id] = customer
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
