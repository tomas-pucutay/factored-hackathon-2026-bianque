# ADR 0004: The language model understands; replies come from templates

- **Status:** accepted, 2026-10-04
- **Code:** [`bianque.agent.llm`](../../src/bianque/agent/llm.py),
  [`bianque.agent.replies`](../../src/bianque/agent/replies.py),
  [`bianque.agent.graph`](../../src/bianque/agent/graph.py)

## Context

The build plan gives the language model two jobs: extract intent and slots into validated
JSON, and write replies from verified facts. The brief adds: only report actions whose outcome
was verified, explanations come from sources, policy rules and execution records, and the
system must resist prompt injection.

## Decision

- **Gemini (`gemini-3.5-flash-lite`) only understands.** It turns the customer's message into
  a validated `Extraction`: intent, yes/no confirmation, language, amount, date, merchant, and
  an injection flag. Inputs are redacted (long digit sequences and emails) and passed as
  delimited data. Bounded retries; a deterministic "1 / 2, yes / no" menu takes over when the
  model is unavailable.
- **Replies are templates** in Spanish and Portuguese, filled only with values read back from
  the tools (case and block numbers, amounts, dates). The model never writes customer-facing
  text.
- **LangGraph** runs the workflow as an explicit state machine; every node is deterministic
  code.

## Why

| Concern | With model-written replies | With templates |
|---|---|---|
| Reporting only verified actions | Needs a checker for every number and claim | By construction: a template can only show tool results |
| Prompt injection | Injected text could steer what the customer is told | The model's output is a fixed schema; it cannot reach the reply |
| Evaluation | Replies vary run to run | Replies are reproducible; scenarios assert exact outcomes |
| Latency and cost | Two model calls per turn | One call per turn (about 0.7–1.2 s, a few hundred tokens) |

Model choice: on 7 test messages (Spanish, Portuguese, ambiguous, injection, out of scope),
`gemini-3.5-flash-lite` and `gemini-3.8-flash` gave identical extractions; flash-lite took
1.3 s per message vs 2.5 s.

## Consequences

- Replies are less varied than free text; the tone is set once, per language, in
  `replies.py`.
- Adding a language means adding a template set; Portuguese material is team-generated, as
  the dataset is in Spanish only.
- The model's role is narrow enough to measure precisely (intent accuracy by language and on
  adversarial messages) in the agent evaluation.
