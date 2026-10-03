-- gold.customer_360: one row per customer, as of the last process date in the data.
--
-- A snapshot, not point-in-time: it is for the agent's context, fairness breakdowns and the
-- serving app. Model features come from transaction_features. To keep it safe from leaking
-- into a model, it carries no fraud labels or anything derived from them.
--
-- Windows are relative to as_of_date. Customers without activity get 0 counts and NULL dates.
-- registration_date is not used for tenure: it is random with respect to activity
-- (docs/silver_data_findings.md section 9); first_tx_at gives observed tenure.

WITH as_of AS (
    SELECT max(process_date) AS as_of_date FROM transactions
),

product_stats AS (
    SELECT
        customer_id,
        count(*) AS n_products,
        count(*) FILTER (WHERE product_status = 'Active') AS n_active_products,
        list(DISTINCT product_type ORDER BY product_type) AS product_types,
        bool_or(product_type = 'Tarjeta Crédito') AS has_credit_card,
        bool_or(product_type = 'Tarjeta Débito') AS has_debit_card,
        bool_or(product_type IN ('Préstamo Personal', 'Préstamo Hipotecario')) AS has_loan,
        max(days_past_due) AS max_days_past_due
    FROM products
    GROUP BY customer_id
),

tx_stats AS (
    SELECT
        t.customer_id,
        count(*) AS n_tx_total,
        min(t.transaction_date) AS first_tx_at,
        max(t.transaction_date) AS last_tx_at,
        count(*) FILTER (WHERE t.process_date > a.as_of_date - INTERVAL 90 DAYS) AS n_tx_90d,
        coalesce(sum(t.amount_usd) FILTER (
            WHERE t.process_date > a.as_of_date - INTERVAL 90 DAYS), 0) AS usd_90d,
        median(t.amount_usd) AS median_ticket_usd,
        count(DISTINCT t.transaction_country) AS n_tx_countries,
        mode(t.channel) AS main_tx_channel
    FROM transactions AS t, as_of AS a
    GROUP BY t.customer_id
),

interaction_stats AS (
    SELECT
        i.customer_id,
        count(*) AS n_interactions_total,
        count(*) FILTER (WHERE i.process_date > a.as_of_date - INTERVAL 90 DAYS) AS n_interactions_90d,
        max(i.interaction_date) AS last_interaction_at,
        avg(i.sentiment_score) FILTER (
            WHERE i.process_date > a.as_of_date - INTERVAL 90 DAYS) AS avg_sentiment_90d,
        count(*) FILTER (WHERE i.was_escalated) AS n_escalations_total,
        mode(i.channel) AS main_contact_channel
    FROM call_center_interactions AS i, as_of AS a
    GROUP BY i.customer_id
),

complaint_stats AS (
    SELECT
        k.customer_id,
        count(*) AS n_complaints_total,
        count(*) FILTER (WHERE k.process_date > a.as_of_date - INTERVAL 365 DAYS) AS n_complaints_365d,
        count(*) FILTER (WHERE k.status IN ('Open', 'In Process', 'Escalated')) AS n_open_complaints,
        bool_or(k.reception_channel = 'Regulator') AS has_regulator_complaint,
        max(k.creation_date) AS last_complaint_at
    FROM complaints AS k, as_of AS a
    GROUP BY k.customer_id
),

survey_stats AS (
    SELECT
        customer_id,
        avg(main_score) FILTER (WHERE survey_type = 'CSAT') AS avg_csat,
        arg_max(nps_category, survey_date) FILTER (WHERE survey_type = 'NPS') AS last_nps_category
    FROM satisfaction_surveys
    GROUP BY customer_id
),

digital_stats AS (
    SELECT
        d.customer_id,
        count(*) FILTER (
            WHERE d.event_type = 'Login' AND d.process_date > a.as_of_date - INTERVAL 30 DAYS
        ) AS n_logins_30d,
        max(d.event_date) FILTER (WHERE d.event_type = 'Login') AS last_login_at,
        mode(d.channel) AS main_digital_channel
    FROM digital_events AS d, as_of AS a
    WHERE d.customer_id IS NOT NULL
    GROUP BY d.customer_id
),

marketing_stats AS (
    SELECT
        s.customer_id,
        count(*) FILTER (WHERE s.process_date > a.as_of_date - INTERVAL 365 DAYS) AS n_sends_365d,
        -- Open rate only over sends with open tracking (Email, Push, SMS).
        avg(s.was_opened::INTEGER) FILTER (
            WHERE s.process_date > a.as_of_date - INTERVAL 365 DAYS AND s.was_opened IS NOT NULL
        ) AS open_rate_365d,
        count(*) FILTER (WHERE s.had_conversion) AS n_conversions_total
    FROM campaign_sends AS s, as_of AS a
    GROUP BY s.customer_id
)

SELECT
    c.customer_id,
    a.as_of_date,
    -- Profile (PII already tokenized in silver)
    c.segment,
    c.customer_status,
    c.country,
    c.state,
    c.city,
    c.age_band,
    c.gender,
    c.detected_accent,
    c.accepts_marketing,
    c.credit_score,
    c.estimated_monthly_income,
    -- Products
    coalesce(p.n_products, 0) AS n_products,
    coalesce(p.n_active_products, 0) AS n_active_products,
    p.product_types,
    coalesce(p.has_credit_card, false) AS has_credit_card,
    coalesce(p.has_debit_card, false) AS has_debit_card,
    coalesce(p.has_loan, false) AS has_loan,
    p.max_days_past_due,
    -- Transactions
    coalesce(t.n_tx_total, 0) AS n_tx_total,
    t.first_tx_at,
    t.last_tx_at,
    date_diff('day', t.first_tx_at::DATE, a.as_of_date) AS observed_tenure_days,
    coalesce(t.n_tx_90d, 0) AS n_tx_90d,
    coalesce(t.usd_90d, 0) AS usd_90d,
    t.median_ticket_usd,
    coalesce(t.n_tx_countries, 0) AS n_tx_countries,
    t.main_tx_channel,
    -- Service
    coalesce(i.n_interactions_total, 0) AS n_interactions_total,
    coalesce(i.n_interactions_90d, 0) AS n_interactions_90d,
    i.last_interaction_at,
    i.avg_sentiment_90d,
    coalesce(i.n_escalations_total, 0) AS n_escalations_total,
    i.main_contact_channel,
    coalesce(k.n_complaints_total, 0) AS n_complaints_total,
    coalesce(k.n_complaints_365d, 0) AS n_complaints_365d,
    coalesce(k.n_open_complaints, 0) AS n_open_complaints,
    coalesce(k.has_regulator_complaint, false) AS has_regulator_complaint,
    k.last_complaint_at,
    s.avg_csat,
    s.last_nps_category,
    -- Digital
    coalesce(d.n_logins_30d, 0) AS n_logins_30d,
    d.last_login_at,
    d.main_digital_channel,
    -- Marketing
    coalesce(m.n_sends_365d, 0) AS n_sends_365d,
    m.open_rate_365d,
    coalesce(m.n_conversions_total, 0) AS n_conversions_total
FROM customers AS c
CROSS JOIN as_of AS a
LEFT JOIN product_stats AS p USING (customer_id)
LEFT JOIN tx_stats AS t USING (customer_id)
LEFT JOIN interaction_stats AS i USING (customer_id)
LEFT JOIN complaint_stats AS k USING (customer_id)
LEFT JOIN survey_stats AS s USING (customer_id)
LEFT JOIN digital_stats AS d USING (customer_id)
LEFT JOIN marketing_stats AS m USING (customer_id)
