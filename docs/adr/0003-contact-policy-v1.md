# ADR 0003: Contact policy v1: the expected-value rule decides, with guardrails

- **Status:** accepted, 2026-10-04
- **Policy:** [`policies/contact_policy_v1.yaml`](../../policies/contact_policy_v1.yaml) (SYNTHETIC)
- **Evidence:** [`reports/policy_comparison.md`](../../reports/policy_comparison.md) (`make evaluate`)

## Context

The calibrated model gives three probabilities: 0.0003 (score ≤ 30), 0.00105 (no score) and
0.9997 (score > 30). The expected-value rule `p × amount > channel cost + friction` turns
that into a **threshold per charge**, (channel cost + friction) / amount: about 0.02 for a
USD 100 charge and 0.0003 for USD 7,000. Large charges clear it even at the lowest
probability, so the rule also sends "was this you?" alerts to many legitimate customers.

The question was whether to add a floor on the probability on top of that rule, and how
much a false alert costs. The team set the friction of a false alert **near USD 2**: at the
scale of a retail bank it is a short message answered with one tap.

## Decision

`contact_policy_v1`, run by `bianque.policy.engine` (the same code in the API and in the
evaluation):

| Rule | Value | Why |
|---|---|---|
| Probability threshold | Per charge: (channel cost + friction) / amount; **no extra floor** (`min_p_fraud: 0`) | No floor beats it on validation at any friction from USD 1.50 to 2.50 |
| Friction | USD 2 per legitimate customer alerted (`cost_assumptions_v1`) | Team assumption, checked from USD 1.50 to 2.50 |
| Abstention | Human review when the credible interval straddles the break-even **and** the stake, (p_high − p_low) × amount, is at least USD 1.11 (one agent call) | Near-ties with little at stake are decided automatically: both choices cost about the same |
| Contact cap | 1 proactive alert per customer per 24 h | Later charges join the open case |
| Channels | Push for app users, otherwise SMS (cheapest per message read) | Real-time only; email is slow; WhatsApp and Voice have no measured response |
| Escalation | A **dispute** ("not mine") goes to a human for amounts ≥ USD 5,000 or customers with ≥ 2 complaints in 365 days | The alert is always automated; "it's mine" closes without a human |
| Actions | Dispute and card block need an authenticated session and the customer's "not mine"; the block also needs explicit confirmation | Enforced by the tools |

### Assumptions (all synthetic, stated in the policy file and the report)

- A false alert costs a legitimate customer USD 2 (friction).
- A contacted fraud's loss is fully avoided.
- A legitimate customer answers "it's mine" and the case closes without a human.
- A human case costs one outbound agent call, USD 1.11 (measured minutes × synthetic rate).
- Customer context (app user, complaints) comes from the `customer_360` snapshot; it routes
  and picks the channel, it does not detect.

## Consequences (offline simulation)

| | Validation | Test |
|---|---:|---:|
| Frauds caught | 412 / 699 | 379 / 603 |
| Automated alerts | 79,100 | 72,717 |
| Legitimate customers alerted | 78,688 (10.6% of legitimate transactions) | 72,338 |
| Cases for a human | 58 | 45 |
| Net benefit | USD 700,301 | USD 589,763 |
| vs the EV rule with no guardrails | −USD 19,588 [−49,560, +687] | −USD 427 [−2,854, +822] |
| vs a 0.01 floor (no false alerts) | +USD 67,475 [−11,727, +143,744] | +USD 34,838 [−29,575, +111,379] |

- **The guardrails cost almost nothing.** Abstention, escalation and the cap cost USD 427 on
  test and keep 99.9% of the cases automated.
- **The case for no floor rests on point estimates.** It wins at every friction checked
  (USD 1.50–2.50), but the bootstrap intervals against a 0.01 floor include 0. If the bank
  judged a false alert to cost more than about USD 3, a floor of 0.01 (only scores above 30,
  zero false alerts, 375 vs 412 frauds) would be the choice. It is one line in the YAML.
- **Alert volume:** about 430 automated "was this you?" messages per day across 150,000
  customers (79,100 in 184 validation days), roughly one per customer per year.
- **Fairness:** fraud recall, false-alert rate and human share are similar across segments,
  countries and age bands (report, last table; groups with few frauds are noisy). All
  customers in the data are in Spanish-speaking countries, so language fairness is measured
  in the agent evaluation, with team-generated Portuguese conversations.

## Alternatives rejected

| Alternative | Why not |
|---|---|
| Floor 0.01 (only scores above 30) | 37 fewer frauds caught on validation; pays only if a false alert costs more than about USD 3 |
| Floor 0.001 (also skip the score ≤ 30 block) | Lower net benefit at every friction checked |
| Abstain on any straddling interval | Sent about 16,000 validation near-ties, with cents at stake, to humans |
| Escalate at alert time | Sent 66,000 alerts (almost all legitimate) to agents; a legitimate "it's mine" needs no human |
| Email, WhatsApp, Voice for alerts | Email too slow for fraud; the other two have no measured response to rank |
