import pytest
from agent_fixtures import make_tools
from fastapi.testclient import TestClient
from test_agent import Scripted

from bianque.agent.graph import Agent
from bianque.api import conversations
from bianque.api.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    agent = Agent(make_tools(tmp_path), Scripted())
    monkeypatch.setattr(conversations, "get_agent", lambda: agent)
    conversations._owners.clear()
    return TestClient(app)


def login(client, customer):
    token = client.post("/test/sessions", json={"customer_id": customer}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_full_proactive_conversation_over_http(client):
    auth = login(client, "CLI-A")

    start = client.post(
        "/conversations/proactive", json={"transaction_id": "TX-A-FRAUD"}, headers=auth
    )
    cid = start.json()["conversation_id"]
    turn = client.post(
        f"/conversations/{cid}/messages", json={"message": "no fui yo"}, headers=auth
    )
    done = client.post(f"/conversations/{cid}/messages", json={"message": "sí"}, headers=auth)
    view = client.get(f"/conversations/{cid}", headers=auth).json()

    assert start.status_code == 200 and "USD 120.00" in start.json()["reply"]
    assert turn.json()["stage"] == "await_block_confirmation"
    assert [a["action"] for a in done.json()["actions"]] == ["open_dispute", "provisional_block"]
    assert done.json()["latency_ms"] >= 0
    assert [t["role"] for t in view["transcript"]] == [
        "bianque",
        "customer",
        "bianque",
        "customer",
        "bianque",
    ]
    assert view["audit"]


def test_unknown_customer_cannot_get_a_test_session(client):
    assert client.post("/test/sessions", json={"customer_id": "CLI-NOPE"}).status_code == 404


def test_starting_without_a_session_is_refused(client):
    r = client.post("/conversations/proactive", json={"transaction_id": "TX-A-FRAUD"})

    assert r.status_code == 401


def test_another_customer_cannot_see_or_continue_a_conversation(client):
    a, b = login(client, "CLI-A"), login(client, "CLI-B")
    cid = client.post(
        "/conversations/proactive", json={"transaction_id": "TX-A-FRAUD"}, headers=a
    ).json()["conversation_id"]

    assert client.get(f"/conversations/{cid}", headers=b).status_code == 404
    other = client.post(f"/conversations/{cid}/messages", json={"message": "no fui yo"}, headers=b)
    assert other.status_code == 404
