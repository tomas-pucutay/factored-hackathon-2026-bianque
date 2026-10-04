# ADR 0003: Contact policy v1 trades simulated net benefit for no false alarms

- **Status:** accepted, 2026-10-04 (thresholds open to team review)
- **Policy:** [`policies/contact_policy_v1.yaml`](../../policies/contact_policy_v1.yaml) (SYNTHETIC)
- **Evidence:** [`reports/policy_comparison.md`](../../reports/policy_comparison.md) (`make evaluate`)

## Context

The calibrated model gives three probabilities: 0.0003 (score ≤ 30), 0.00105 (no score) and
0.9997 (score > 30). The expected-value rule alone (`p × amount > channel cost + friction`)
also contacts every **legitimate-looking** charge large enough to clear the hurdle at those
tiny probabilities: over about USD 6,800 at 0.0003 and USD 2,000 at 0.00105. On validation
that is 79,065 legitimate customers contacted, about 11% of all legitimate transactions, to
catch 40 more frauds. Whether that pays depends entirely on the synthetic USD 2 friction.

## Decision

`contact_policy_v1`, run by `bianque.policy.engine` (the same code in the API and in the
evaluation):

| Rule | Value | Why |
|---|---|---|
| Minimum probability | 0.01 | Only the score > 30 block (p ≈ 1) is contacted proactively |
| Expected value | p × amount > channel + USD 2 friction | The rule from the solution design |
| Abstention | Credible interval straddles the break-even → human review | Model uncertainty goes to a person |
| Contact cap | 1 proactive contact per customer per 24 h | Later charges join the open case |
| Channels | Push for app users, otherwise SMS (cheapest per message read) | Real-time channels only; email is slow; WhatsApp and Voice have no measured response |
| Human handles the case | Amount ≥ USD 5,000 or ≥ 2 complaints in 365 days | About 10% of flagged charges; about 1% of customers |
| Actions | Dispute and card block need an authenticated session and the customer's "not mine"; the block also needs explicit confirmation | Enforced by the tools |

## Consequences (offline simulation)

| | Validation | Test |
|---|---:|---:|
| Frauds caught (v1 vs EV rule only) | 375 vs 415 of 699 | 347 vs 380 of 603 |
| Legitimate customers contacted | **0** vs 79,065 | **0** vs 72,737 |
| Cases for a human | 37 | 29 |
| Automated share of handled cases | 0.90 | 0.92 |
| Net benefit vs EV rule only | −USD 87,062 [−165,342, −9,949] | −USD 35,265 [−106,787, +29,627] |

- **The policy gives up simulated net benefit** (significant on validation, not on test) to
  send **no false fraud alerts**. It wins as soon as a false alert costs a legitimate customer
  more than **USD 3.10** (USD 2.61 against a 0.001 minimum, which contacts 40,928 legitimate
  customers). A false fraud alert means worry, a call to the bank and possibly a blocked
  card, so we judge USD 2 too low. "The best complaint is the one that never arrives" applies
  to legitimate customers too.
- **Without the minimum, escalation would flood the human team.** The high-amount rule would
  send 66,470 validation cases (almost all legitimate) to agents.
- Frauds below the minimum (scores ≤ 30 and unscored) are left to the reactive path, where
  the customer reports the charge.
- Recall and human share by segment, country and age band are in the report. All customers in
  the data are in Spanish-speaking countries, so language fairness is measured in the agent
  evaluation, with team-generated Portuguese conversations.

## Alternatives rejected

| Alternative | Why not |
|---|---|
| EV rule only | 79,065 false alerts on validation; pays only if a false alert costs less than USD 3.10 |
| Minimum 0.001 | 40,928 false alerts (all unscored transactions over about USD 2,000) |
| Email, WhatsApp, Voice for alerts | Email too slow for fraud; the other two have no measured response to rank |
| A model-uncertainty band as the main abstention | The intervals are narrow; abstention comes mostly from the escalation rules |
