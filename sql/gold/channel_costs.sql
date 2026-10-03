-- gold.channel_costs: cost and response by contact channel, from campaign_sends.
--
-- The cost side of the expected-value rule: contact only when
-- p(fraud) x amount > channel cost + friction for a legitimate customer.
--
-- send_cost has no currency in the data dictionary; it is identical across Argentina,
-- Colombia and Mexico (e.g. SMS 0.10, Voice 0.20), so it is treated as USD.
-- Rates use the right denominator: delivery over sends; open, click and conversion over
-- delivered sends. Voice and WhatsApp have no open tracking (docs/silver_data_findings.md
-- section 7), and their clicks and conversions are always false (0 of 400k sends), which
-- means untracked, not zero response. For them opens_tracked is false and open, click and
-- conversion rates are NULL; their response must come from an assumption, not from 0.

SELECT
    send_channel AS channel,
    count(*) AS n_sends,
    min(process_date) AS period_start,
    max(process_date) AS period_end,
    'USD' AS cost_currency,
    count(send_cost) AS n_sends_with_cost,
    avg(send_cost) AS avg_cost_per_send,
    median(send_cost) AS median_cost_per_send,
    avg(was_delivered::INTEGER) AS delivery_rate,
    sum(send_cost) / nullif(count(*) FILTER (WHERE was_delivered AND send_cost IS NOT NULL), 0)
        AS cost_per_delivered,
    count(was_opened) FILTER (WHERE was_delivered) > 0 AS opens_tracked,
    avg(was_opened::INTEGER) FILTER (WHERE was_delivered) AS open_rate,
    CASE WHEN count(was_opened) FILTER (WHERE was_delivered) > 0
        THEN avg(was_clicked::INTEGER) FILTER (WHERE was_delivered) END AS click_rate,
    CASE WHEN count(was_opened) FILTER (WHERE was_delivered) > 0
        THEN avg(had_conversion::INTEGER) FILTER (WHERE was_delivered) END AS conversion_rate,
    median(epoch(open_date - send_date) / 3600.0) FILTER (WHERE was_opened) AS median_hours_to_open
FROM campaign_sends
GROUP BY send_channel
