-- amount_usd for silver.transactions.
--
-- The source converts with a fixed booking rate per currency (in this dataset 350 ARS and
-- 4,000 COP per USD), not with daily_exchange_rates: that rate reproduces 99.9% of the
-- source values. NULLs are filled with the same rate so every row uses one method. The rate
-- is measured from the rows that have amount_usd, as whole units per USD, not hardcoded.
--
-- amount_usd_source records how each value was obtained:
--   source        value came from the source system
--   usd_amount    USD transaction: amount_usd = amount (the source leaves these NULL)
--   booking_rate  filled with the measured booking rate
--   NULL          no booking rate for that currency (no source rows to measure it)

WITH units_per_usd AS (
    SELECT
        currency,
        round(median(amount / amount_usd)) AS units
    FROM input
    WHERE amount_usd IS NOT NULL
      AND amount_usd <> 0
      AND currency <> 'USD'
    GROUP BY currency
)

SELECT
    i.* REPLACE (
        coalesce(
            i.amount_usd,
            CASE
                WHEN i.currency = 'USD' THEN i.amount
                ELSE CAST(round(i.amount / u.units, 2) AS DECIMAL(15, 2))
            END
        ) AS amount_usd
    ),
    CASE
        WHEN i.amount_usd IS NOT NULL THEN 'source'
        WHEN i.currency = 'USD' THEN 'usd_amount'
        WHEN u.units IS NOT NULL THEN 'booking_rate'
    END AS amount_usd_source
FROM input AS i
LEFT JOIN units_per_usd AS u USING (currency)
