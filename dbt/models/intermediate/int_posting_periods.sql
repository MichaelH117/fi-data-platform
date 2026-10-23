{{ config(materialized='table') }}
-- Accounting periods (calendar months) and the period-lock rule (docs/V3_DESIGN.md 2.5).
-- A period closes at the end of business day N (var period_close_business_day, weekdays only)
-- of the following month. is_closed is evaluated at the reporting as-of date.
{% set n = var('period_close_business_day') | int %}
{% set fy = var('fiscal_year_start_month') | int %}

with months as (
    select cast(unnest(generate_series(date '2018-01-01', date '2037-12-01', interval 1 month)) as date)
        as period_start
),

next_month_days as (
    select
        period_start,
        cast(unnest(generate_series(
            period_start + interval 1 month, period_start + interval 2 month - interval 1 day, interval 1 day
        )) as date) as day
    from months
),

close_days as (
    select period_start, day as close_date
    from next_month_days
    where isodow(day) <= 5
    qualify row_number() over (partition by period_start order by day) = {{ n }}
)

select
    {{ yyyymm('m.period_start') }} as posting_period_key,
    m.period_start,
    cast(last_day(m.period_start) as date) as period_end,
    c.close_date,
    a.as_of_date > c.close_date as is_closed,
    -- Fiscal calendar (fiscal year named by the calendar year it ends in).
    year(m.period_start) + case when {{ fy }} > 1 and month(m.period_start) >= {{ fy }} then 1 else 0 end
        as fiscal_year,
    ((month(m.period_start) - {{ fy }} + 12) % 12) + 1 as fiscal_month,
    (((month(m.period_start) - {{ fy }} + 12) % 12) // 3) + 1 as fiscal_quarter,
    strftime(m.period_start, '%Y-%m') as period_label
from months as m
inner join close_days as c using (period_start)
cross join {{ ref('int_as_of_date') }} as a
