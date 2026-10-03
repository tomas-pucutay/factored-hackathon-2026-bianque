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
dedupe_order: [_ingested_at DESC, _source_key DESC]  # latest row per primary key wins

columns:
  amount: {type: "DECIMAL(15,2)", nullable: false, range: [0, null]}
  channel: {type: VARCHAR, allowed: [ATM, App, Branch]}

value_map:                     # normalizations applied before checks
  country: {México: Mexico}

foreign_keys:
  - {column: customer_id, references: customers.customer_id}            # on_orphan: quarantine (default)
  - {column: registration_branch_id, references: branches.branch_id, on_orphan: nullify}

pii:
  hash: [email]                # HMAC-SHA256 with PII_HASH_KEY
  age_band: [date_of_birth]    # replaced by an age band
  free_text: [full_text]       # may contain PII, cannot be tokenized; documented only
```

- `nullable` defaults to `true`. `range` bounds are inclusive; `null` means open-ended.
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
- `México` and `Mexico` are mixed across tables; `value_map` normalizes them to `Mexico`.
- `daily_exchange_rates` covers all 12 currency pairs per day (13,164 rows, not 3,000).
- Mexican customers' products are all in USD; there are no MXN products or transactions.
- `complaints.origin_interaction_id` is 100% null.
- No duplicate primary keys exist in any table, despite the documented ~2%.
- `customers.registration_branch_id` and `service_agents.assigned_branch_id` are random IDs,
  not dirty keys, so they are nullified (see the Silver section of the project README).
