-- Calendar dimension (role-playing: entered, effective, expiration, loss, posted dates).
-- Also the MetricFlow time spine. Fiscal year/quarter follow var fiscal_year_start_month.
{% set fy = var('fiscal_year_start_month') | int %}
with days as (
    select cast(unnest(generate_series(date '2015-01-01', date '2037-12-31', interval 1 day)) as date) as date_day
)

select
    {{ date_key('date_day') }} as date_key,
    date_day,
    date_day as "date",
    cast(date_trunc('month', date_day) as date) as month_start,
    cast(date_trunc('quarter', date_day) as date) as quarter_start,
    {{ yyyymm('date_day') }} as posting_period_key,
    year(date_day) as year,
    quarter(date_day) as quarter,
    month(date_day) as month,
    strftime(date_day, '%b') as month_name,
    strftime(date_day, '%Y-%m') as year_month,
    year(date_day) + case when {{ fy }} > 1 and month(date_day) >= {{ fy }} then 1 else 0 end as fiscal_year,
    (((month(date_day) - {{ fy }} + 12) % 12) // 3) + 1 as fiscal_quarter,
    isodow(date_day) as iso_day_of_week,
    isodow(date_day) >= 6 as is_weekend,
    date_day = last_day(date_day) as is_month_end
from days
