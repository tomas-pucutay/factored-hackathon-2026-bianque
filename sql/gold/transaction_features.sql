-- gold.transaction_features: one row per transaction with point-in-time features.
--
-- Point-in-time rule: every feature of a transaction at time t uses only information with a
-- timestamp strictly before t. Windows end at "1 microsecond preceding", so a transaction
-- never sees itself or another transaction at the same instant, and event counts use
-- ASOF joins on "event time < t".
--
-- Deliberately NOT used as features:
--   - is_fraud of earlier transactions: labels are known only after investigation.
--   - product/customer snapshot fields (balance, status, credit_score, last_*): they reflect
--     the export date, not the time of the transaction.
--   - digital_events.product_id and complaints.affected_product_id: they point to products of
--     other customers (docs/silver_data_findings.md section 9).
--   - customers.registration_date and products.opening_date: random with respect to activity
--     (36% of customers transact before registering). Tenure is observed instead:
--     days_since_first_tx.
--
-- source_fraud_score is the bank's existing score, kept as the baseline to beat.
-- is_fraud is the label.

WITH tx AS (
    SELECT
        transaction_id, customer_id, product_id, transaction_date, process_date, process_month,
        amount_usd, currency, channel, transaction_type, transaction_category,
        transaction_country, merchant_name, is_fraud, fraud_score
    FROM transactions
),

history AS (
    SELECT
        *,
        epoch(transaction_date - lag(transaction_date) OVER w_seq) / 3600.0 AS hours_since_prev_tx,
        count(*) OVER w_prior AS n_prior_tx,
        count(*) OVER w_24h AS n_tx_24h,
        count(*) OVER w_7d AS n_tx_7d,
        count(*) OVER w_30d AS n_tx_30d,
        coalesce(sum(amount_usd) OVER w_24h, 0) AS sum_usd_24h,
        coalesce(sum(amount_usd) OVER w_7d, 0) AS sum_usd_7d,
        coalesce(sum(amount_usd) OVER w_30d, 0) AS sum_usd_30d,
        median(amount_usd) OVER w_prior AS prior_median_usd,
        epoch(transaction_date - min(transaction_date) OVER w_prior) / 86400.0 AS days_since_first_tx,
        max(amount_usd) OVER w_prior AS prior_max_usd,
        -- First time this customer uses a country / merchant / channel (earlier rows only).
        count(*) OVER (
            PARTITION BY customer_id, transaction_country ORDER BY transaction_date
            RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL 1 MICROSECOND PRECEDING
        ) = 0 AS is_new_country,
        CASE WHEN merchant_name IS NOT NULL THEN count(*) OVER (
            PARTITION BY customer_id, merchant_name ORDER BY transaction_date
            RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL 1 MICROSECOND PRECEDING
        ) = 0 END AS is_new_merchant,
        count(*) OVER (
            PARTITION BY customer_id, channel ORDER BY transaction_date
            RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL 1 MICROSECOND PRECEDING
        ) = 0 AS is_new_channel
    FROM tx
    WINDOW
        w_seq AS (PARTITION BY customer_id ORDER BY transaction_date, transaction_id),
        w_prior AS (
            PARTITION BY customer_id ORDER BY transaction_date
            RANGE BETWEEN UNBOUNDED PRECEDING AND INTERVAL 1 MICROSECOND PRECEDING
        ),
        w_24h AS (
            PARTITION BY customer_id ORDER BY transaction_date
            RANGE BETWEEN INTERVAL 24 HOURS PRECEDING AND INTERVAL 1 MICROSECOND PRECEDING
        ),
        w_7d AS (
            PARTITION BY customer_id ORDER BY transaction_date
            RANGE BETWEEN INTERVAL 7 DAYS PRECEDING AND INTERVAL 1 MICROSECOND PRECEDING
        ),
        w_30d AS (
            PARTITION BY customer_id ORDER BY transaction_date
            RANGE BETWEEN INTERVAL 30 DAYS PRECEDING AND INTERVAL 1 MICROSECOND PRECEDING
        )
),

-- Cumulative event counts per customer (ties share the count), for ASOF lookups:
-- events in (t - window, t) = cum(last event < t) - cum(last event <= t - window).
complaint_events AS (
    SELECT customer_id, creation_date AS ts,
           count(*) OVER (PARTITION BY customer_id ORDER BY creation_date RANGE UNBOUNDED PRECEDING) AS cum
    FROM complaints
),
interaction_events AS (
    SELECT customer_id, interaction_date AS ts,
           count(*) OVER (PARTITION BY customer_id ORDER BY interaction_date RANGE UNBOUNDED PRECEDING) AS cum
    FROM call_center_interactions
),
login_events AS (
    SELECT customer_id, event_date AS ts,
           count(*) OVER (PARTITION BY customer_id ORDER BY event_date RANGE UNBOUNDED PRECEDING) AS cum
    FROM digital_events
    WHERE event_type = 'Login' AND customer_id IS NOT NULL
),
error_events AS (
    SELECT customer_id, event_date AS ts,
           count(*) OVER (PARTITION BY customer_id ORDER BY event_date RANGE UNBOUNDED PRECEDING) AS cum
    FROM digital_events
    WHERE event_type = 'Error' AND customer_id IS NOT NULL
),

events AS (
    SELECT
        h.transaction_id,
        coalesce(c1.cum, 0) - coalesce(c0.cum, 0) AS n_complaints_90d,
        coalesce(i1.cum, 0) - coalesce(i0.cum, 0) AS n_interactions_30d,
        coalesce(l1.cum, 0) - coalesce(l0.cum, 0) AS n_logins_24h,
        coalesce(e1.cum, 0) - coalesce(e0.cum, 0) AS n_digital_errors_24h
    FROM history AS h
    ASOF LEFT JOIN complaint_events AS c1
        ON h.customer_id = c1.customer_id AND h.transaction_date > c1.ts
    ASOF LEFT JOIN complaint_events AS c0
        ON h.customer_id = c0.customer_id AND h.transaction_date - INTERVAL 90 DAYS >= c0.ts
    ASOF LEFT JOIN interaction_events AS i1
        ON h.customer_id = i1.customer_id AND h.transaction_date > i1.ts
    ASOF LEFT JOIN interaction_events AS i0
        ON h.customer_id = i0.customer_id AND h.transaction_date - INTERVAL 30 DAYS >= i0.ts
    ASOF LEFT JOIN login_events AS l1
        ON h.customer_id = l1.customer_id AND h.transaction_date > l1.ts
    ASOF LEFT JOIN login_events AS l0
        ON h.customer_id = l0.customer_id AND h.transaction_date - INTERVAL 24 HOURS >= l0.ts
    ASOF LEFT JOIN error_events AS e1
        ON h.customer_id = e1.customer_id AND h.transaction_date > e1.ts
    ASOF LEFT JOIN error_events AS e0
        ON h.customer_id = e0.customer_id AND h.transaction_date - INTERVAL 24 HOURS >= e0.ts
)

SELECT
    -- Keys and time
    h.transaction_id,
    h.customer_id,
    h.product_id,
    h.transaction_date,
    h.process_date,
    -- Transaction
    h.amount_usd,
    h.currency,
    h.channel,
    h.transaction_type,
    h.transaction_category,
    h.transaction_country,
    h.transaction_country <> c.country AS is_foreign,
    hour(h.transaction_date) AS hour_of_day,
    isodow(h.transaction_date) AS day_of_week,
    isodow(h.transaction_date) >= 6 AS is_weekend,
    hour(h.transaction_date) < 6 AS is_night,
    -- Customer history (strictly before)
    h.hours_since_prev_tx,
    h.days_since_first_tx,
    h.n_prior_tx,
    h.n_tx_24h,
    h.n_tx_7d,
    h.n_tx_30d,
    h.sum_usd_24h,
    h.sum_usd_7d,
    h.sum_usd_30d,
    h.prior_median_usd,
    h.amount_usd / nullif(h.prior_median_usd, 0) AS amount_to_prior_median,
    h.amount_usd > h.prior_max_usd AS exceeds_prior_max,
    h.is_new_country,
    h.is_new_merchant,
    h.is_new_channel,
    -- Other activity before the transaction
    ev.n_complaints_90d,
    ev.n_interactions_30d,
    ev.n_logins_24h,
    ev.n_digital_errors_24h,
    -- Stable customer and product attributes
    c.segment,
    c.country AS customer_country,
    c.age_band,
    p.product_type,
    -- Baseline and label
    h.fraud_score AS source_fraud_score,
    h.is_fraud,
    h.process_month
FROM history AS h
JOIN events AS ev USING (transaction_id)
JOIN customers AS c ON c.customer_id = h.customer_id
JOIN products AS p ON p.product_id = h.product_id
