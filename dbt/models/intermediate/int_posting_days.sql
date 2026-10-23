{{ config(materialized='table') }}
-- Day -> first posting period still open on that day. An event recorded on `date_day` can post
-- no earlier than `first_open_period_key` (macro posting_period_key). The previous month stays
-- open until its close date (business day N of this month); after that only this month is open.
with days as (
    select cast(unnest(generate_series(date '2018-01-01', date '2037-12-31', interval 1 day)) as date) as date_day
)

select
    d.date_day,
    case
        when prev.close_date is not null and d.date_day <= prev.close_date then prev.posting_period_key
        else {{ yyyymm('d.date_day') }}
    end as first_open_period_key
from days as d
left join {{ ref('int_posting_periods') }} as prev
    on prev.period_start = cast(date_trunc('month', d.date_day) - interval 1 month as date)
