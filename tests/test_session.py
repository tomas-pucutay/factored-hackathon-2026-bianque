import pytest

from bianque.agent.session import SessionError, issue, verify


@pytest.fixture(autouse=True)
def secret(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret")


def test_a_valid_token_proves_the_customer():
    token = issue("CLI-1", ttl_seconds=60, now=1_000)

    session = verify(token, now=1_030)

    assert session.customer_id == "CLI-1"


def test_expired_token_is_rejected():
    token = issue("CLI-1", ttl_seconds=60, now=1_000)

    with pytest.raises(SessionError, match="expired"):
        verify(token, now=1_060)


def test_tampered_token_is_rejected():
    body, signature = issue("CLI-1", now=1_000).rsplit(".", 1)
    forged = issue("CLI-2", now=1_000).rsplit(".", 1)[0]

    with pytest.raises(SessionError, match="invalid"):
        verify(f"{forged}.{signature}", now=1_001)
    with pytest.raises(SessionError, match="invalid"):
        verify(f"{body}.{'0' * 64}", now=1_001)


def test_missing_token_or_customer_number_is_not_a_session():
    for token in (None, "", "CLI-1"):
        with pytest.raises(SessionError, match="no session"):
            verify(token)


def test_token_signed_with_another_secret_is_rejected(monkeypatch):
    token = issue("CLI-1", now=1_000)
    monkeypatch.setenv("SESSION_SECRET", "other-secret")

    with pytest.raises(SessionError, match="invalid"):
        verify(token, now=1_001)
