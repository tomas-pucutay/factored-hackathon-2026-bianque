"""Trusted test sessions: the only proof of identity the agent accepts.

The brief requires authentication through a trusted test session: a customer number or a
national ID typed in a chat does not prove who is writing. Here the bank's app login is
simulated by a signed token (HMAC-SHA256 with SESSION_SECRET) that carries the customer and
an expiry. The agent and the tools only trust a token that verifies and has not expired;
whatever the customer writes about their identity is ignored.

Tokens are issued by a test-only endpoint of the API (POST /test/sessions), never by the
conversation itself.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass

DEFAULT_TTL_SECONDS = 15 * 60


class SessionError(Exception):
    """The session is missing, forged or expired."""


@dataclass(frozen=True)
class Session:
    customer_id: str
    expires_at: float

    def expired(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) >= self.expires_at


def _secret() -> bytes:
    secret = os.getenv("SESSION_SECRET")
    if not secret:
        raise SessionError("SESSION_SECRET is not set (see .env.example)")
    return secret.encode()


def _sign(payload: bytes, secret: bytes) -> str:
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def issue(
    customer_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS, now: float | None = None
) -> str:
    """A signed token for customer_id, valid for ttl_seconds."""
    issued = time.time() if now is None else now
    payload = json.dumps({"sub": customer_id, "exp": issued + ttl_seconds}).encode()
    body = base64.urlsafe_b64encode(payload).decode()
    return f"{body}.{_sign(payload, _secret())}"


def verify(token: str | None, now: float | None = None) -> Session:
    """The session behind a token, or SessionError. Constant-time signature check."""
    if not token or "." not in token:
        raise SessionError("no session: the customer must log in through the bank's app")
    body, signature = token.rsplit(".", 1)
    try:
        payload = base64.urlsafe_b64decode(body.encode())
    except ValueError as e:
        raise SessionError("malformed session token") from e
    if not hmac.compare_digest(_sign(payload, _secret()), signature):
        raise SessionError("invalid session token")
    claims = json.loads(payload)
    session = Session(customer_id=claims["sub"], expires_at=float(claims["exp"]))
    if session.expired(now):
        raise SessionError("session expired: the customer must log in again")
    return session
