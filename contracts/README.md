# Silver schema contracts

One YAML file per table. A contract describes the **silver output** of that table and is the
single source of truth for types, keys, relationships and PII. It is based on the LATAM Bank
Data Dictionary v1.0.0, corrected where the bronze data disagrees with it.

## Format

```yaml
table: transactions            # table name, same as the bronze dataset
kind: fact                     # fact | dimension | reference
description: ...
primary_key: [transaction_id]
partition_column: process_date # facts only: drives the late-arrival window
process_day:                   # facts only: how the source assigns process_date (checked, never recomputed)
  timestamp: transaction_date  # events before the cutoff belong to the previous process day
  cutoff: "06:00"
  tolerance_minutes: 0         # optional clock skew around the cutoff
  # or, for child tables: inherits: {table: call_center_interactions, via: interaction_id}
dedupe_order: [_ingested_at DESC, _source_key DESC]  # latest row per primary key wins

columns:
  amount: {type: "DECIMAL(15,2)", nullable: false, range: [0, null]}
  channel: {type: VARCHAR, allowed: [ATM, App, Branch]}
  languages: {type: "VARCHAR[]", split: ", ", allowed: [en, es, pt]}  # list column
  credit_limit:                # structural NULL: must be NULL when any condition holds
    type: "DECIMAL(15,2)"
    null_when: [{product_type: [Cuenta Ahorro, Seguro]}, {product_status: [Closed]}]

value_map:                     # normalizations applied before checks, only where observed
  country: {México: Mexico}
  languages: {español: es}     # on a list column, applied to each element

foreign_keys:
  - {column: customer_id, references: customers.customer_id}            # on_orphan: quarantine (default)
  - {column: registration_branch_id, references: branches.branch_id, on_orphan: nullify}

pii:
  hash: [email]                # HMAC-SHA256 with PII_HASH_KEY
  age_band: [date_of_birth]    # replaced by an age band
  free_text: [full_text]       # may contain PII, cannot be tokenized; documented only
```

- `nullable` defaults to `true`. `range` bounds are inclusive; `null` means open-ended.
- `null_when` lists conditions (OR between list items) where the value does not apply and
  must be NULL; a non-NULL there is a quality failure. Outside those conditions a NULL is
  missing data and is reported. `null` inside a condition matches NULL values.
- `split` turns a delimited text column into a list; `allowed` then applies to each element.
- `value_map` is only added for variants actually observed in bronze, not speculatively.
- `allowed` and `range` are quality checks. Type casts and `nullable: false` are hard rules.

## Rules

| Rule | Behavior |
|------|----------|
| Column in data, not in contract | Allowed (additive change), logged |
| Column in contract, missing in data | Build fails (breaking change) |
| Value that cannot be cast to its type | Row goes to quarantine |
| FK orphan, `on_orphan: quarantine` | Row goes to `data/silver/_quarantine/<table>/` with a reason |
| FK orphan, `on_orphan: nullify` | Row is kept, the FK is set to NULL and counted in the quality report |

Integer columns arrive as floats in the CSVs (`701.0`), so they are cast through `DOUBLE`.

## Deviations from the data dictionary

Observed in bronze on 2026-10-03:

- Category values are partly in Spanish (`product_type`, `reason_category`,
  `detected_sentiment`, `geographic_zone`, `document_type: Pasaporte`). Contracts use the
  observed values; silver does not translate them.
- `México` and `Mexico` are mixed in `customers.country`, `branches.country`,
  `transactions.transaction_country`, `digital_events.ip_country` and
  `campaign_sends.open_country`; `value_map` normalizes them to `Mexico`.
- `service_agents.languages` is a comma-separated list of Spanish names
  (`español, inglés, portugués`); silver turns it into a list of ISO codes (`[es, en, pt]`).
- `process_date` is a business day, not the calendar date of the event, and the rule differs
  per table (see `process_day` in each fact contract): 06:00 cutoff for transactions,
  campaign sends and digital events (up to 10 minutes of clock skew), 08:00 cutoff for
  call center interactions and complaints, and inherited from the interaction for surveys and
  transcripts. Silver keeps the source `process_date` as the partition and watermark key.
- `campaign_sends.subject` contains `"¡Oferta especial en nan!"` in 38,142 rows, a pandas NaN
  leaked from campaigns without a promoted product; it becomes NULL. A scan of every text
  column in all tables found no other leaked placeholders, blank strings or stray whitespace.
- NULLs come in two layers: structural (the field does not apply, e.g. `credit_limit` on a
  savings account, `browser` on an app event, `resolution_date` on an open complaint) and
  random on top of that, ~5% in most columns as the dictionary states. Structural rules are
  encoded with `null_when` and were checked against every row in bronze.
- `campaign_sends.was_opened` is NULL by design for undelivered sends and for Voice and
  WhatsApp, which have no open tracking; it is not coerced to false.
- `call_center_interactions.duration_seconds` is NULL for Chat and Email, and
  `wait_time_seconds` exists only for Inbound Call. `call_transcripts.duration_seconds` is a
  copy of the interaction's, so it is NULL for chat and email transcripts.
- `transactions.amount_usd` is NULL for every USD transaction (plus ~5% random elsewhere);
  silver sets it to `amount` for USD and converts the rest with `daily_exchange_rates`.
- `digital_events` rows with `ip_country = 'Mexico'` (no accent, 1.04M rows) are anonymous
  traffic with no `customer_id` and no `ip_city`; after normalization they are identified by
  `customer_id IS NULL`.
- `daily_exchange_rates` covers all 12 currency pairs per day (13,164 rows, not 3,000).
- Mexican customers' products are all in USD; there are no MXN products or transactions.
- `complaints.origin_interaction_id` is 100% null.
- No duplicate primary keys exist in any table, despite the documented ~2%.
- `customers.registration_branch_id` and `service_agents.assigned_branch_id` are random IDs,
  not dirty keys, so they are nullified (see the Silver section of the project README).
