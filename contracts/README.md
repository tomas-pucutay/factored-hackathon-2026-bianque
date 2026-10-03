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

derived:                       # columns computed in silver, not present in bronze
  amount_usd_source: {type: VARCHAR, allowed: [source, usd_amount, exchange_rate]}

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

The contracts differ from the data dictionary wherever bronze disagrees with it (Spanish
category values, orphaned branch keys, business-day `process_date`, structural NULLs, list
columns, leaked placeholders). Each deviation, with its evidence and how silver handles it, is
documented in [`docs/silver_data_findings.md`](../docs/silver_data_findings.md).
