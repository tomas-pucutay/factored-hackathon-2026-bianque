-- gold.agent_routing: who can take a handoff, by language and specialty.
--
-- Bianque resolves in Spanish and Portuguese and hands off to a human with the right
-- language and specialty. Languages come from silver as ISO codes (['es', 'pt', ...]).
-- Performance is measured from the agent's interactions and surveys in the last 90 days of
-- data, next to the profile's own avg_csat (a field of the source dimension).
-- assigned_branch_id is not used: it is a random ID (docs/silver_data_findings.md section 3).

WITH as_of AS (
    SELECT max(process_date) AS as_of_date FROM call_center_interactions
),

performance AS (
    SELECT
        i.agent_id,
        count(*) AS n_interactions_90d,
        avg(i.was_resolved::INTEGER) AS first_contact_resolution_rate_90d,
        avg(i.was_escalated::INTEGER) AS escalation_rate_90d,
        avg(i.sentiment_score) AS avg_sentiment_90d
    FROM call_center_interactions AS i, as_of AS a
    WHERE i.agent_id IS NOT NULL AND i.process_date > a.as_of_date - INTERVAL 90 DAYS
    GROUP BY i.agent_id
),

surveys AS (
    SELECT
        s.agent_id,
        avg(s.main_score) FILTER (WHERE s.survey_type = 'CSAT') AS measured_csat_90d,
        count(*) FILTER (WHERE s.survey_type = 'CSAT') AS n_csat_surveys_90d
    FROM satisfaction_surveys AS s, as_of AS a
    WHERE s.agent_id IS NOT NULL AND s.process_date > a.as_of_date - INTERVAL 90 DAYS
    GROUP BY s.agent_id
)

SELECT
    g.agent_id,
    a.as_of_date,
    g.languages,
    list_contains(g.languages, 'es') AS speaks_es,
    list_contains(g.languages, 'pt') AS speaks_pt,
    list_contains(g.languages, 'en') AS speaks_en,
    g.native_accent,
    g.country_of_origin,
    g.agent_type,
    g.specialty,
    coalesce(g.specialty = 'Fraudes', false) AS is_fraud_specialist,
    coalesce(g.specialty = 'Quejas y Reclamos', false) AS is_complaints_specialist,
    g.experience_level,
    g.agent_status,
    g.agent_status = 'Active' AS is_available,
    g.work_shift,
    g.avg_csat AS profile_avg_csat,
    coalesce(p.n_interactions_90d, 0) AS n_interactions_90d,
    p.first_contact_resolution_rate_90d,
    p.escalation_rate_90d,
    p.avg_sentiment_90d,
    s.measured_csat_90d,
    coalesce(s.n_csat_surveys_90d, 0) AS n_csat_surveys_90d
FROM service_agents AS g
CROSS JOIN as_of AS a
LEFT JOIN performance AS p USING (agent_id)
LEFT JOIN surveys AS s USING (agent_id)
