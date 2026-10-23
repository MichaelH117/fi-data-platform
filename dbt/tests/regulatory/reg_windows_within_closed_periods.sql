-- Regulatory windows never run into an open posting period.
-- MARKET_SURVEY: every window (the current year is year-to-date) ends on or before the last closed
-- posting period end, the same reporting date every other figure uses.
-- FUNDED_RESERVE: the regulatory year is set by the state, so a window may end in a period that has not
-- closed yet (just after the year end), but then it must be flagged preliminary (is_final = false).
-- Returns offending windows; zero rows passes.
with last_closed as (
    select p.period_end
    from {{ ref('dim_reporting_date') }} as r
    inner join {{ ref('dim_posting_period') }} as p on p.posting_period_key = r.last_closed_period_key
)

select w.report_id, w.state, w.window_id, w.window_end, w.is_final, c.period_end as last_closed_end
from {{ ref('mart_regulatory_windows') }} as w
cross join last_closed as c
where w.window_end > c.period_end
  and (w.report_id = 'MARKET_SURVEY' or w.is_final)
