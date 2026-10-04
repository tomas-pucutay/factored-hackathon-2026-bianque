# Policies and assumptions

Everything in this folder is **synthetic: written by the team, not measured in the
dataset**. Files are versioned (`_v1`, `_v2`, …): a version that produced published results
is never edited; a change is a new file, and the active version is chosen in
`configs/settings.yaml`.

| File | What it defines | Used by |
|------|-----------------|---------|
| `cost_assumptions_v1.yaml` | Cost per service contact by interaction type; friction cost of contacting a legitimate customer | `gold.service_cost_baseline`, the expected-value contact rule |
| `contact_policy_v1.yaml` | When to contact proactively (minimum probability, expected value, abstention, contact cap), which channel, when a human takes the case, and what each action requires | `bianque.policy.engine`, the API, `make evaluate` |
