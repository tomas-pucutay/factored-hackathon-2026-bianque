"""Contact policy: what to do with a calibrated fraud probability.

Separation of concerns: the model outputs p_fraud (with its credible interval); this module
decides, deterministically, from a versioned YAML policy (policies/contact_policy_v*.yaml,
labeled SYNTHETIC) and the measured channel costs. The language model never decides.

Every Decision lists the rules that produced it (rule id + the numbers behind it), so the
explanation of any contact comes from policy rules and execution records, not from a model's
reasoning.

Rules, in order:
  1. min_probability    p_fraud below the policy minimum: no contact.
  2. uncertain          the credible interval of p straddles the break-even: human review.
  3. expected_value     p x amount <= channel cost + friction: no contact.
  4. contact_cap        the customer was contacted within the cap window: no new contact.
  5. contact            otherwise contact, through the cheapest eligible real-time channel;
                        handled by a human when the amount is high or the customer is a
                        repeat complainer, by Bianque otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

Action = Literal["contact", "no_contact", "human_review"]
APP_CHANNELS = frozenset({"Android App", "iOS App"})


@dataclass(frozen=True)
class Policy:
    version: str
    cost_assumptions: str
    friction_usd: float
    min_p_fraud: float
    abstain_when_interval_straddles: bool
    max_contacts_per_customer_hours: float
    allowed_channels: tuple[str, ...]
    eligibility: dict[str, str]
    high_amount_usd: float
    repeat_complainer_365d: int
    on_unclear_intent_turns: int
    actions: dict[str, tuple[str, ...]]


def load_policy(path: Path, cost_assumptions_path: Path) -> Policy:
    """Load a policy and the friction cost from the cost assumptions it names."""
    raw = yaml.safe_load(path.read_text())
    costs = yaml.safe_load(cost_assumptions_path.read_text())
    if costs["version"] != raw["cost_assumptions"]:
        raise ValueError(
            f"{path} expects {raw['cost_assumptions']}, but {cost_assumptions_path} is "
            f"{costs['version']}"
        )
    contact, channels, esc = raw["contact"], raw["channels"], raw["escalation"]
    unknown = set(channels["allowed"]) - set(channels["eligibility"])
    if unknown:
        raise ValueError(f"{path}: allowed channels without an eligibility rule: {unknown}")
    return Policy(
        version=raw["version"],
        cost_assumptions=raw["cost_assumptions"],
        friction_usd=float(costs["friction_cost_legit_usd"]),
        min_p_fraud=float(contact["min_p_fraud"]),
        abstain_when_interval_straddles=bool(contact["abstain_when_interval_straddles"]),
        max_contacts_per_customer_hours=float(contact["max_contacts_per_customer_hours"]),
        allowed_channels=tuple(channels["allowed"]),
        eligibility=dict(channels["eligibility"]),
        high_amount_usd=float(esc["high_amount_usd"]),
        repeat_complainer_365d=int(esc["repeat_complainer_365d"]),
        on_unclear_intent_turns=int(esc["on_unclear_intent_turns"]),
        actions={name: tuple(a["requires"]) for name, a in raw["actions"].items()},
    )


@dataclass(frozen=True)
class ChannelStats:
    """One row of gold.channel_costs."""

    channel: str
    cost_per_delivered: float
    open_rate: float | None  # None when the channel has no response tracking

    @property
    def cost_per_read(self) -> float | None:
        return self.cost_per_delivered / self.open_rate if self.open_rate else None


@dataclass(frozen=True)
class Charge:
    transaction_id: str
    amount_usd: float
    p_fraud: float
    p_fraud_low: float | None = None
    p_fraud_high: float | None = None


@dataclass(frozen=True)
class CustomerContext:
    main_digital_channel: str | None
    complaints_365d: int
    hours_since_last_contact: float | None = None  # None: never contacted

    @property
    def app_user(self) -> bool:
        return self.main_digital_channel in APP_CHANNELS


@dataclass(frozen=True)
class Reason:
    rule: str
    detail: str


@dataclass(frozen=True)
class Decision:
    action: Action
    channel: str | None
    handled_by: Literal["bianque", "human"] | None
    expected_loss_usd: float
    hurdle_usd: float
    reasons: tuple[Reason, ...]
    policy_version: str


def choose_channel(
    policy: Policy, customer: CustomerContext, channels: dict[str, ChannelStats]
) -> tuple[ChannelStats, Reason]:
    """Cheapest cost per message read among the allowed channels the customer can receive."""
    eligible = [
        channels[name]
        for name in policy.allowed_channels
        if name in channels
        and channels[name].cost_per_read is not None
        and (policy.eligibility[name] == "always" or customer.app_user)
    ]
    if not eligible:
        raise ValueError("no eligible channel: check the policy and gold.channel_costs")
    best = min(eligible, key=lambda c: c.cost_per_read)
    others = ", ".join(f"{c.channel} {c.cost_per_read:.4f}" for c in eligible if c is not best)
    detail = f"{best.channel}: USD {best.cost_per_read:.4f} per message read"
    return best, Reason("channel", detail + (f" (vs {others})" if others else ""))


def decide(
    policy: Policy,
    charge: Charge,
    customer: CustomerContext,
    channels: dict[str, ChannelStats],
) -> Decision:
    channel, channel_reason = choose_channel(policy, customer, channels)
    hurdle = channel.cost_per_delivered + policy.friction_usd
    expected = charge.p_fraud * charge.amount_usd
    ev = Reason(
        "expected_value",
        f"p_fraud {charge.p_fraud:.4f} x USD {charge.amount_usd:,.2f} = USD {expected:,.2f} "
        f"vs channel USD {channel.cost_per_delivered:.4f} + friction USD {policy.friction_usd:.2f}",
    )

    def done(action: Action, reasons: list[Reason], handled_by=None, ch=None) -> Decision:
        return Decision(action, ch, handled_by, expected, hurdle, tuple(reasons), policy.version)

    if charge.p_fraud < policy.min_p_fraud:
        return done(
            "no_contact",
            [Reason("min_probability", f"p_fraud {charge.p_fraud:.4f} < {policy.min_p_fraud}")],
        )
    lo, hi = charge.p_fraud_low, charge.p_fraud_high
    if (
        policy.abstain_when_interval_straddles
        and lo is not None
        and hi is not None
        and lo * charge.amount_usd <= hurdle < hi * charge.amount_usd
    ):
        return done(
            "human_review",
            [
                Reason(
                    "uncertain",
                    f"credible interval [{lo:.4f}, {hi:.4f}] x USD {charge.amount_usd:,.2f} "
                    f"straddles the hurdle USD {hurdle:.2f}",
                ),
                ev,
            ],
            handled_by="human",
        )
    if expected <= hurdle:
        return done("no_contact", [ev])
    since = customer.hours_since_last_contact
    if since is not None and since < policy.max_contacts_per_customer_hours:
        return done(
            "no_contact",
            [
                ev,
                Reason(
                    "contact_cap",
                    f"contacted {since:.1f} h ago (< {policy.max_contacts_per_customer_hours:g} h): "
                    "joins the open case",
                ),
            ],
        )
    escalations = []
    if charge.amount_usd >= policy.high_amount_usd:
        escalations.append(
            Reason("high_amount", f"USD {charge.amount_usd:,.2f} >= {policy.high_amount_usd:,.0f}")
        )
    if customer.complaints_365d >= policy.repeat_complainer_365d:
        escalations.append(
            Reason(
                "repeat_complainer",
                f"{customer.complaints_365d} complaints in 365 days "
                f">= {policy.repeat_complainer_365d}",
            )
        )
    return done(
        "contact",
        [ev, channel_reason, *escalations],
        handled_by="human" if escalations else "bianque",
        ch=channel.channel,
    )


def missing_requirements(policy: Policy, action: str, facts: set[str]) -> list[str]:
    """Requirements of an action (policy.actions) not met by the verified facts; [] = allowed."""
    if action not in policy.actions:
        raise KeyError(f"action {action!r} is not defined in {policy.version}")
    return [r for r in policy.actions[action] if r not in facts]
