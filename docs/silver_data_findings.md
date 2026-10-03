# Silver data findings

Knowledge base of what the bronze data actually looks like and how silver handles it. Every
finding was measured on 100% of the rows in bronze (2026-10-03, 7,671 files) unless noted, and
every rule here is encoded in [`contracts/`](../contracts/) and was validated against bronze.

Use this document before touching silver, gold or the quality checks: it explains why a
contract differs from the [data dictionary](../contracts/README.md) and which NULLs are
expected.

## Contents

1. [Row counts vs the dictionary](#1-row-counts-vs-the-dictionary)
2. [Primary keys and duplicates](#2-primary-keys-and-duplicates)
3. [Foreign keys and orphans](#3-foreign-keys-and-orphans)
4. [Value normalization](#4-value-normalization)
5. [List columns](#5-list-columns)
6. [Process date business rules](#6-process-date-business-rules)
7. [Structural NULLs](#7-structural-nulls)
8. [Other source quirks](#8-other-source-quirks)
9. [How these were found](#9-how-these-were-found)

## 1. Row counts vs the dictionary

| Table | Bronze rows | Dictionary | Note |
|-------|------------:|-----------:|------|
| customers | 150,000 | 150,000 | |
| products | 400,000 | 400,000 | |
| branches | 350 | 350 | |
| service_agents | 1,200 | 1,200 | |
| marketing_campaigns | 200 | 200 | |
| daily_exchange_rates | 13,164 | 3,000 | 12 currency pairs × 1,097 days, all complete |
| transactions | 4,425,008 | 5,000,000 | |
| call_center_interactions | 686,296 | 800,000 | |
| call_transcripts | 171,321 | 200,000 | |
| satisfaction_surveys | 212,759 | 250,000 | |
| complaints | 67,095 | 80,000 | |
| campaign_sends | 1,746,801 | 2,000,000 | |
| digital_events | 15,620,994 | 10,000,000 | |

Facts hold ~85% of the documented rows (1.56× for `digital_events`). Row counts are not a
quality check; report what exists.

## 2. Primary keys and duplicates

No table has duplicate primary keys, despite the documented ~2%. Dedupe is still built
(`dedupe_order` in each contract) and is exercised by the team-generated fixture.

- Dimensions with `last_updated` (customers, products): latest `last_updated` wins.
- Everything else has no update timestamp: latest `_ingested_at`, then `_source_key`, wins.

## 3. Foreign keys and orphans

All 24 relationships listed in the dictionary were checked.

| Relationship | Orphans | Policy |
|--------------|--------:|--------|
| `customers.registration_branch_id → branches` | 149,995 / 150,000 (100%) | `nullify` |
| `service_agents.assigned_branch_id → branches` | 831 / 833 non-null (99.8%) | `nullify` |
| All other 22 | 0 | `quarantine` |

`complaints.origin_interaction_id` is 100% NULL, so its relationship is never exercised.

### The two orphaned branch keys are random IDs, not dirty keys

Before choosing a policy we checked whether they could be repaired:

| Check | Result |
|-------|--------|
| Format (length 12, `SUC-` prefix, whitespace, case) | Identical to `branches.branch_id` |
| Cardinality | 150,000 distinct values for 150,000 customers; 833 for 833 agents. A real FK repeats (~430 customers per branch) |
| Normalizing `O→0`, `I→1`, removing dashes | Still only the same 5 matches |
| Edit distance to the nearest real `branch_id` | 98% are 5–6 characters away out of 8; a typo would be 1–2 |
| Match against `branch_code` | 0 |
| Match against the branch of the customer's own products | 0 of 400,000 |
| `products.opening_branch_id` | Uses exactly the 350 branches, 0 orphans: the generator could produce valid keys |

The information to repair them does not exist. Silver sets them to NULL and keeps the rows:

- Keeping the random IDs would let an inner join with `branches` silently drop every customer.
- Quarantining would empty `customers` and `service_agents`.
- The original values stay in bronze; the quality report counts the nullified keys.
- For geography and fairness, use `customers.country` / `customers.city`, not the branch.

## 4. Value normalization

`value_map` is applied only where the variant was observed, never speculatively.

| Table | Column | Mapping | Note |
|-------|--------|---------|------|
| customers | `country` | `México → Mexico` | |
| branches | `country` | `México → Mexico` | |
| campaign_sends | `open_country` | `México → Mexico` | |
| transactions | `transaction_country` | `México → Mexico` | Both spellings present |
| digital_events | `ip_country` | `México → Mexico` | Both spellings present |
| service_agents | `languages` | `español → es`, `inglés → en`, `portugués → pt` | Per list element |
| campaign_sends | `subject` | `"¡Oferta especial en nan!" → NULL` | 38,142 rows, see §8 |

Where both spellings exist, the unaccented `Mexico` marks a different generator path:

- `transactions`: `Mexico` has 40,515 rows, the same volume as `Brazil`, `Spain` and `USA`
  (~40.5k each), so it looks like the "international transactions" path. `México` has 2.1M.
- `digital_events`: all 1,038,174 rows with `Mexico` are anonymous traffic (100% NULL
  `customer_id` and `ip_city`). After normalization they are identified by
  `customer_id IS NULL`, so no information is lost.

`service_agents.country_of_origin` and `marketing_campaigns.target_country` only contain
`Mexico` and have no mapping.

Category values are partly in Spanish (`product_type`, `reason_category`,
`detected_sentiment`, `geographic_zone: Urbana`, `document_type: Pasaporte`). Silver keeps
the observed values and does not translate them.

## 5. List columns

Delimited text columns become `VARCHAR[]` in silver (`split` in the contract).

| Table | Column | Source example | Delimiter | Silver |
|-------|--------|----------------|-----------|--------|
| service_agents | `languages` | `español, inglés, portugués` | `", "` (comma + space) | `[es, en, pt]` |
| call_center_interactions | `mentioned_products` | `PRD-A,PRD-B` | `","` (comma, no space) | `[PRD-A, PRD-B]` |
| call_transcripts | `detected_keywords` | `banco, cuenta` | `", "` (comma + space) | `[banco, cuenta]` |

`languages` drives handoff routing by language, so it uses ISO 639-1 codes.

## 6. Process date business rules

`process_date` is a business day, not the calendar date of the event. Events before a
cutoff hour belong to the previous process day. The rule differs per table (`process_day` in
each contract) and holds for 100% of rows:

| Table | Rule | Evidence |
|-------|------|----------|
| transactions | Cutoff 06:00 on `transaction_date` | 25% of rows fall on the previous day, all with hour 00–05 |
| campaign_sends | Cutoff 06:00 on `send_date` | Same pattern |
| digital_events | Cutoff 06:00 on `event_date`, up to 10 min tolerance | See below |
| call_center_interactions | Cutoff 08:00 on `interaction_date` | Contact center hours |
| complaints | Cutoff 08:00 on `creation_date` | |
| satisfaction_surveys | Inherits the `process_date` of its interaction via `interaction_id` | 100% equal; only 40% match the survey's own date |
| call_transcripts | Inherits the `process_date` of its interaction via `interaction_id` | 100% equal |

The cutoff is the same in every country (including Brazil, Spain and USA transactions), so
it is not a time zone effect.

**digital_events:** events between 06:00 and 06:10 are split between the previous and the
same day, with a gradual handover (06:00 → 80% previous day, 06:05 → 11%, 06:10 → 0%). This
happens within the same day on 1,017 of 1,098 days, so it is not a cutoff that varies by
day. The likely cause is that `event_date` comes from the client device clock, ahead of the
server clock that assigns `process_date` by up to ~10 minutes.

Implications:

- Silver never recomputes `process_date`; the source value is the partition and the
  late-arrival watermark key.
- Joins by "day" between tables must use `process_date`, not the event date.
- No partition arrives late in the real data; late arrivals are only tested with the fixture.

## 7. Structural NULLs

NULLs come in two layers:

1. **Structural:** the field does not apply to that row. Encoded as `null_when` in the
   contracts; a non-NULL value there is a quality failure.
2. **Random, on top:** ~5% in most columns (as the dictionary states), sometimes 10–20%.
   Reported as missing data; never filled.

Structural NULLs are never filled or coerced (e.g. `was_opened` is not turned into `false`).

### products

| Column | NULL when | Random elsewhere |
|--------|-----------|-----------------:|
| `credit_limit` | `product_type` not a credit product (Cuenta Ahorro, Cuenta Corriente, Inversión, Seguro, Tarjeta Débito) | 5% |
| `days_past_due` | Same as `credit_limit` | 5% |
| `expiration_date` | `product_type` not a card (everything except Tarjeta Crédito, Tarjeta Débito) | 5% |
| `last_transaction_date` | `product_status` in Blocked, Closed, Suspended | 10% |

### transactions

| Column | NULL when | Random elsewhere |
|--------|-----------|-----------------:|
| `amount_usd` | `currency = USD` (silver fills it with `amount`) | 5% (silver fills it with the booking rate, see §8) |
| `transaction_category` | `transaction_type` in Adjustment, Deposit, Transfer, Withdrawal | 5% |
| `merchant_name`, `merchant_category` | `transaction_type` is not Purchase | 5% |
| `branch_id` | `channel` in App, POS, Transfer, Web | 5% |
| `latitude`, `longitude` | `channel` in App, Transfer, Web | 71.5% on ATM, Branch, POS |

### call_center_interactions

Both rules are fully structural, with no random NULLs:

| Column | NULL when |
|--------|-----------|
| `duration_seconds` | `interaction_type` in Chat, Email (asynchronous, no duration) |
| `wait_time_seconds` | `interaction_type` is not Inbound Call (queue wait exists only for inbound calls) |

### call_transcripts

| Column | Rule |
|--------|------|
| `duration_seconds` | A copy of the interaction's `duration_seconds` (equal in 100% of the 147,292 rows where both exist). NULL for transcripts of Chat and Email interactions. NOT NULL in the dictionary; nullable in the contract |

### satisfaction_surveys

| Column | NULL when | Random elsewhere |
|--------|-----------|-----------------:|
| `nps_category` | `survey_type` in CES, CSAT | 5% |

Not structural: questions 1–3 appear in every combination; `question_n_text` and
`question_n_response` and `open_comments` / `comment_sentiment` are NULL independently.

### complaints

Lifecycle fields exist only once the case reaches the matching `status`:

| Column | NULL when `status` in | Random elsewhere |
|--------|-----------------------|-----------------:|
| `assigned_agent_id`, `assignment_date` | Open, Rejected | 5% |
| `first_response_date` | Escalated, Open, Rejected | 5% |
| `resolution_date`, `resolution_days`, `resolution` | Escalated, In Process, Open, Rejected | 5% |
| `compensation_granted` | Escalated, In Process, Open, Rejected | ~71% on Resolved and Closed |
| `closing_date` | Every status except Closed | 5% |
| `resolution_satisfaction` | Every status except Closed | 5% |
| `claimed_amount`, `currency` | `case_type` in Request, Suggestion | ~62% on Claim and Complaint |

### campaign_sends

| Column | NULL when | Random elsewhere |
|--------|-----------|-----------------:|
| `subject` | `send_channel` is not Email | 10% |
| `was_opened` | Not delivered, or `send_channel` in Voice, WhatsApp (no open tracking) | 0% |
| `open_date` | `was_opened` is false or NULL | 0% |
| `open_device`, `open_country` | `was_opened` is false or NULL | 10% |
| `click_date`, `click_count` | `was_clicked = false` | 0% |
| `conversion_date`, `conversion_value` | `had_conversion = false` | 0% |
| `failure_reason` | `send_status = Sent` | 5% |

Open rates exist only for Email, Push and SMS.

### digital_events

| Column | NULL when | Random elsewhere |
|--------|-----------|-----------------:|
| `browser` | `channel` in Android App, iOS App | 5% |
| `app_version` | `channel` in Desktop Web, Mobile Web | 5% |
| `product_id` | `event_category` is not Product | ~62% on Product |
| `event_value` | `event_type` in Click, Error, Login, Logout, PageView | 5% |
| `duration_seconds` | `event_type` is not PageView | 5% |
| `referrer`, `utm_*` | `event_type` is not Login (attribution is recorded on login) | ~57–66% on Login |
| `customer_id`, `ip_city` | Source `ip_country = 'Mexico'` (anonymous traffic, see §4) | ~19% / ~23% |

### No structural pattern found

Single-category explanations were tested for every other column with NULLs; none applied.
These are treated as random missing data: e.g. `customers.landline_phone` (50%),
`customers.detected_accent` (30%), `service_agents.specialty` (40%),
`call_center_interactions.mentioned_products` (60%), `marketing_campaigns.target_country`
(56%).

## 8. Other source quirks

| Table | Finding | Handling |
|-------|---------|----------|
| campaign_sends | `subject` = `"¡Oferta especial en nan!"` in 38,142 rows: campaigns without `promoted_product` leaked a pandas NaN into the text. The other 520,317 subjects match `"¡Oferta especial en <promoted_product>!"` exactly | Mapped to NULL (real subject unknown) |
| transactions | `amount_usd` uses a fixed booking rate per currency, not `daily_exchange_rates`: `amount_usd / amount` is 1/350 for ARS and 1/4000 for COP in every quarter of 2023–2026, and that rate reproduces 99.87% (ARS) and 99.99% (COP) of source values to the cent (the rest differ by a rounding cent). No combination of `daily_exchange_rates` (calendar or process date; exchange, buy or sell rate; either direction) matches more than 6.2% exactly. The daily table fluctuates ±2% around the same 350 and 4,000 | NULLs filled with the booking rate measured from the source rows (`sql/silver/transactions.sql`), so every row uses one method; `amount_usd_source` records `source`, `usd_amount` or `booking_rate` |
| campaign_sends | 8,277 sends after the campaign's `end_date` | Quality check, not a contract rule |
| all | Scan of every text column: no other leaked placeholders (`nan`, `None`, `null`, `N/A`, …), blank strings or stray whitespace | — |
| call_transcripts | `mentioned_entities` contains `"products": null` | Valid JSON, not a placeholder |
| several | Integer columns arrive as floats (`701.0`) | Cast through `DOUBLE` |
| products, transactions | Mexican customers' products are all in USD; no product or transaction uses MXN (only `complaints.currency` does) | Kept as is |
| transactions | `fraud_score` range is 0–99.99, consistent with the dictionary (0–100) | — |

## 9. How these were found

Profiling queries were run with DuckDB over `data/bronze/**/*.parquet`:

- **Row counts and PK duplicates:** `count(*)` vs `count(DISTINCT pk)` per table.
- **FK orphans:** left join each child column against the distinct parent keys.
- **Dirty-key repair:** format, cardinality, normalized match, `levenshtein` distance to the
  nearest parent key, and matches against alternative columns.
- **Placeholders:** every text column checked for placeholder values (exact and as a word
  inside text), blanks, and leading/trailing/double whitespace.
- **Process date:** distribution of `process_date - date(event)` and of the event hour, then
  the latest time assigned to the previous day vs the earliest assigned to the same day.
- **Structural NULLs:** for each column with NULLs, every low-cardinality never-NULL column
  was tested as an explainer; a column is structural when its NULLs are 0% or 100% in every
  group. Multi-column hypotheses (e.g. `was_opened` + `was_delivered`) were checked directly.
- **Contracts:** each contract was validated against bronze (columns, allowed values,
  observed `value_map` keys, `nullable`, `null_when` and `process_day` rules). The validator
  was an ad-hoc script; it becomes `tests/test_contracts.py` when silver is implemented.
