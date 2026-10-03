-- gold.dispute_outcomes: one row per complaint with what it cost when it arrived.
--
-- Bianque contacts customers before they complain; this table measures the value side of
-- "the best complaint is the one that never arrives": compensation, SLA breaches, regulator
-- escalations and resolution time.
--
-- Amounts: complaints.currency is random with respect to the amount (claimed amounts are
-- uniform 0-5,000 with the same distribution under ARS, COP, MXN and USD, and the label does
-- not follow the customer's country). Converting by the label would distort amounts up to
-- 4,000x, so amounts are treated as USD (*_usd_assumed) and the label is kept as
-- source_currency_label. See docs/silver_data_findings.md section 9.
--
-- complaints.affected_product_id never belongs to the complaining customer (0 of 44,570);
-- it is not joined, and affected_product_is_customers records the check.

SELECT
    k.complaint_id,
    k.customer_id,
    k.creation_date,
    k.process_date,
    k.case_type,
    k.category,
    k.subcategory,
    k.category IN ('Transactions', 'Fees') AS is_charge_dispute,
    k.reception_channel,
    k.reception_channel = 'Regulator' AS is_regulator,
    k.priority,
    k.status,
    k.status IN ('Resolved', 'Closed') AS is_resolved,
    k.sla_breached,
    epoch(k.first_response_date - k.creation_date) / 3600.0 AS first_response_hours,
    k.resolution_days,
    k.claimed_amount AS claimed_amount_usd_assumed,
    k.compensation_granted AS compensation_usd_assumed,
    k.compensation_granted IS NOT NULL AS has_compensation,
    k.currency AS source_currency_label,
    k.resolution_satisfaction,
    k.is_repeat_complainer,
    k.assigned_agent_id,
    p.customer_id IS NOT DISTINCT FROM k.customer_id AND k.affected_product_id IS NOT NULL
        AS affected_product_is_customers,
    -- For fairness breakdowns
    c.segment,
    c.country,
    c.age_band
FROM complaints AS k
JOIN customers AS c USING (customer_id)
LEFT JOIN products AS p ON p.product_id = k.affected_product_id
