from dataclasses import replace
from pathlib import Path

import pytest

from bianque.policy.engine import (
    ChannelStats,
    Charge,
    CustomerContext,
    decide,
    load_policy,
    missing_requirements,
)

POLICY = load_policy(
    Path("policies/contact_policy_v1.yaml"), Path("policies/cost_assumptions_v1.yaml")
)
CHANNELS = {
    "Push": ChannelStats("Push", 0.0006, 0.40),
    "SMS": ChannelStats("SMS", 0.1063, 0.50),
    "Email": ChannelStats("Email", 0.0059, 0.30),
    "WhatsApp": ChannelStats("WhatsApp", 0.0531, None),
}
APP_USER = CustomerContext("Android App", complaints_365d=0)
WEB_USER = CustomerContext("Desktop Web", complaints_365d=0)


def rules(decision):
    return [r.rule for r in decision.reasons]


def test_policy_file_loads_with_its_cost_assumptions():
    assert POLICY.version == "contact_policy_v1"
    assert POLICY.friction_usd == 2.0
    assert POLICY.allowed_channels == ("Push", "SMS")


def test_policy_refuses_mismatched_cost_assumptions(tmp_path):
    other = tmp_path / "costs.yaml"
    other.write_text("version: cost_assumptions_v9\nfriction_cost_legit_usd: 1\n")
    with pytest.raises(ValueError, match="expects cost_assumptions_v1"):
        load_policy(Path("policies/contact_policy_v1.yaml"), other)


def test_likely_fraud_is_contacted_by_bianque_through_push_for_app_users():
    d = decide(POLICY, Charge("t1", 120.0, 0.9997, 0.998, 1.0), APP_USER, CHANNELS)

    assert (d.action, d.channel, d.handled_by) == ("contact", "Push", "bianque")
    assert rules(d) == ["expected_value", "channel"]
    assert d.hurdle_usd == pytest.approx(0.0006 + 2.0)
    assert d.policy_version == "contact_policy_v1"


def test_customers_without_the_app_get_sms_never_email_or_untracked_channels():
    d = decide(POLICY, Charge("t1", 120.0, 0.9997), WEB_USER, CHANNELS)

    assert d.channel == "SMS"


def test_large_amounts_clear_the_hurdle_even_at_low_probability():
    d = decide(POLICY, Charge("t1", 9_000.0, 0.0003, 0.00028, 0.00033), APP_USER, CHANNELS)

    assert d.action == "contact"  # 0.0003 x 9,000 = 2.70 > 2.0006
    assert d.handled_by == "human"  # a dispute of USD 9,000 goes to an agent


def test_minimum_probability_is_a_floor_on_top_of_the_rule():
    floored = replace(POLICY, min_p_fraud=0.01)

    d = decide(floored, Charge("t1", 9_000.0, 0.0003), APP_USER, CHANNELS)

    assert (d.action, rules(d)) == ("no_contact", ["min_probability"])


def test_expected_value_below_hurdle_is_not_contacted():
    d = decide(POLICY, Charge("t1", 2.0, 0.9997), APP_USER, CHANNELS)  # 2.00 < 2.0006

    assert (d.action, rules(d)) == ("no_contact", ["expected_value"])


def test_interval_straddling_break_even_goes_to_human_review():
    d = decide(POLICY, Charge("t1", 100.0, 0.03, 0.01, 0.05), APP_USER, CHANNELS)

    assert (d.action, d.handled_by) == ("human_review", "human")
    assert rules(d)[0] == "uncertain"  # stake (0.05 - 0.01) x 100 = 4.00 >= 1.11


def test_near_tie_with_a_small_stake_is_decided_automatically():
    # Straddles the hurdle, but (0.00033 - 0.00028) x 7,000 = 0.35 < 1.11: not worth a review.
    d = decide(POLICY, Charge("t1", 7_000.0, 0.0003, 0.00028, 0.00033), APP_USER, CHANNELS)

    assert d.action == "contact"


def test_recent_contact_caps_a_new_message():
    customer = CustomerContext("iOS App", 0, hours_since_last_contact=3.0)

    d = decide(POLICY, Charge("t1", 120.0, 0.9997), customer, CHANNELS)

    assert (d.action, rules(d)) == ("no_contact", ["expected_value", "contact_cap"])


def test_high_amount_and_repeat_complainers_are_handled_by_a_human():
    repeat = CustomerContext("Android App", complaints_365d=2)

    high = decide(POLICY, Charge("t1", 5_000.0, 0.9997), APP_USER, CHANNELS)
    both = decide(POLICY, Charge("t2", 7_500.0, 0.9997), repeat, CHANNELS)

    assert (high.action, high.handled_by) == ("contact", "human")
    assert rules(high)[-1] == "high_amount"
    assert rules(both)[-2:] == ["high_amount", "repeat_complainer"]


def test_actions_require_verified_facts():
    block = "provisional_card_block"

    assert missing_requirements(POLICY, block, {"intent_not_mine"}) == [
        "authenticated_session",
        "explicit_confirmation",
    ]
    facts = {"authenticated_session", "intent_not_mine", "explicit_confirmation"}
    assert missing_requirements(POLICY, block, facts) == []
    with pytest.raises(KeyError):
        missing_requirements(POLICY, "refund_everything", facts)
