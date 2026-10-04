import time

import pytest
from agent_fixtures import make_tools

from bianque.agent.session import Session, SessionError
from bianque.agent.tools import PermissionDenied, RequirementsNotMet, ToolFailure

A = Session("CLI-A", expires_at=time.time() + 600)
B = Session("CLI-B", expires_at=time.time() + 600)
NOT_MINE = {"authenticated_session", "intent_not_mine"}


def test_a_customer_only_sees_their_own_charges(tmp_path):
    tools = make_tools(tmp_path)

    assert tools.get_charge(A, "TX-A-FRAUD").amount_usd == 120.0
    with pytest.raises(PermissionDenied):
        tools.get_charge(A, "TX-B-FRAUD")
    assert {c.transaction_id for c in tools.find_charges(A)} == {
        "TX-A-FRAUD",
        "TX-A-BIG",
        "TX-A-NORMAL",
    }


def test_find_charges_matches_what_the_customer_described(tmp_path):
    from datetime import date

    tools = make_tools(tmp_path)

    by_amount = tools.find_charges(A, amount_usd=120.5)  # within 1%
    rounded = tools.find_charges(A, amount_usd=46)  # USD 45.50, said as a round number
    by_date = tools.find_charges(A, on_date=date(2026, 6, 16))  # within a day
    by_merchant = tools.find_charges(A, merchant="gasolinera")

    assert [c.transaction_id for c in by_amount] == ["TX-A-FRAUD"]
    assert [c.transaction_id for c in rounded] == ["TX-A-NORMAL"]
    assert [c.transaction_id for c in by_date] == ["TX-A-FRAUD"]
    assert [c.transaction_id for c in by_merchant] == ["TX-A-FRAUD"]


def test_dispute_and_block_need_verified_facts(tmp_path):
    tools = make_tools(tmp_path)

    with pytest.raises(RequirementsNotMet):
        tools.open_dispute(A, "TX-A-FRAUD", {"intent_not_mine"})  # not authenticated
    case = tools.open_dispute(A, "TX-A-FRAUD", NOT_MINE)
    with pytest.raises(RequirementsNotMet):
        tools.block_product(A, "TX-A-FRAUD", case["case_id"], NOT_MINE)  # no confirmation
    block = tools.block_product(
        A, "TX-A-FRAUD", case["case_id"], NOT_MINE | {"explicit_confirmation"}
    )

    assert tools.verify_case(A, case["case_id"])["status"] == "open"
    assert tools.verify_block(A, block["block_id"])["product_id"] == "PRD-A1"
    assert tools.open_dispute(A, "TX-A-FRAUD", NOT_MINE) == case  # idempotent


def test_another_customer_cannot_act_on_or_read_a_case(tmp_path):
    tools = make_tools(tmp_path)
    case = tools.open_dispute(A, "TX-A-FRAUD", NOT_MINE)

    with pytest.raises(PermissionDenied):
        tools.open_dispute(B, "TX-A-FRAUD", NOT_MINE)
    with pytest.raises(PermissionDenied):
        tools.verify_case(B, case["case_id"])
    with pytest.raises(PermissionDenied):
        tools.block_product(B, "TX-B-FRAUD", case["case_id"], NOT_MINE | {"explicit_confirmation"})


def test_its_mine_is_stored_as_a_label(tmp_path):
    tools = make_tools(tmp_path)

    label = tools.close_as_legitimate(A, "TX-A-FRAUD", {"authenticated_session", "intent_its_mine"})

    assert (label["label"], label["source"]) == ("legitimate", "customer")


def test_expired_session_and_injected_failures_are_refused(tmp_path):
    expired = Session("CLI-A", expires_at=time.time() - 1)

    with pytest.raises(SessionError):
        make_tools(tmp_path).get_charge(expired, "TX-A-FRAUD")
    with pytest.raises(ToolFailure):
        make_tools(tmp_path, fail=frozenset({"open_dispute"})).open_dispute(
            A, "TX-A-FRAUD", NOT_MINE
        )


def test_policy_decision_and_handoff_routing(tmp_path):
    tools = make_tools(tmp_path)

    decision = tools.policy_decision(A, "TX-A-FRAUD")

    assert (decision.action, decision.channel) == ("contact", "Push")
    assert tools.route_agent("pt")["agent_id"] == "AGT-PT"  # fraud specialist first
    assert tools.route_agent("es")["agent_id"] == "AGT-ES"
