-- gold.service_cost_baseline: what reactive customer service costs today, by contact reason
-- and interaction type. The status-quo baseline for the ROI comparison.
--
-- Volumes, handling times, first-contact resolution and escalation come from the data.
-- Cost does not: it uses the SYNTHETIC assumptions in policies/cost_assumptions_v*.yaml
-- (contact_cost_assumptions), and every row records which version it used.
--   - Calls and video: measured minutes x cost per minute.
--   - Chat and email: no duration in the data (structural NULL), so a flat cost per contact.
-- contact_reason always equals reason_category in the source, so only the category is kept.

WITH period AS (
    SELECT
        min(process_date) AS period_start,
        max(process_date) AS period_end,
        date_diff('day', min(process_date), max(process_date)) / 30.4375 + 1 / 30.4375 AS months
    FROM call_center_interactions
),

contacts AS (
    SELECT
        i.reason_category,
        i.interaction_type,
        i.duration_seconds,
        i.wait_time_seconds,
        i.was_resolved,
        i.was_escalated,
        coalesce(i.duration_seconds / 60.0 * a.cost_per_minute_usd, a.cost_per_contact_usd)
            AS contact_cost_usd,
        a.assumptions_version
    FROM call_center_interactions AS i
    LEFT JOIN contact_cost_assumptions AS a USING (interaction_type)
)

SELECT
    c.reason_category,
    c.interaction_type,
    any_value(p.period_start) AS period_start,
    any_value(p.period_end) AS period_end,
    count(*) AS n_contacts,
    count(*) / any_value(p.months) AS contacts_per_month,
    median(c.duration_seconds) / 60.0 AS median_handle_minutes,
    avg(c.duration_seconds) / 60.0 AS avg_handle_minutes,
    median(c.wait_time_seconds) / 60.0 AS median_wait_minutes,
    avg(c.was_resolved::INTEGER) AS first_contact_resolution_rate,
    avg(c.was_escalated::INTEGER) AS escalation_rate,
    avg(c.contact_cost_usd) AS cost_per_contact_usd,
    sum(c.contact_cost_usd) AS total_cost_usd,
    sum(c.contact_cost_usd) / any_value(p.months) AS cost_per_month_usd,
    count(*) FILTER (WHERE c.contact_cost_usd IS NULL) AS n_contacts_without_cost,
    any_value(c.assumptions_version) AS assumptions_version
FROM contacts AS c
CROSS JOIN period AS p
GROUP BY c.reason_category, c.interaction_type
