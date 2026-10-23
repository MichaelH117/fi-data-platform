-- Reporting windows per regulatory report and state (grain: report_id x state x window_id).
-- FUNDED_RESERVE: the latest complete regulatory year (starts on the state's period_start_month,
-- for example Sep-Aug) plus the ITD window that runs from inception to the same period end.
-- Only states with has_requirement get filing windows; the rest are skipped.
-- MARKET_SURVEY: one window per calendar year (the last var market_survey_years years) for states that
-- asked for the survey. The current year is year-to-date and ends at the last closed posting period end
-- (the reporting date every other figure stops at), never the as-of date, so no open-period activity is
-- filed and the YTD column can be reproduced from closed-period reports. A current year with no closed
-- period yet (as of early January) has no window.
-- is_final: the window end falls in a closed posting period (otherwise the figures are preliminary).
with as_of as (
    select r.as_of_date, p.period_end as last_closed_end
    from {{ ref('dim_reporting_date') }} as r
    left join {{ ref('dim_posting_period') }} as p on p.posting_period_key = r.last_closed_period_key
),

methods as (
    select * from {{ ref('state_reserve_method') }}
),

filing_end as (
    select
        m.state,
        case
            when make_date(year(a.as_of_date), m.period_start_month, 1) - interval 1 day <= a.as_of_date
                then cast(make_date(year(a.as_of_date), m.period_start_month, 1) - interval 1 day as date)
            else cast(make_date(year(a.as_of_date) - 1, m.period_start_month, 1) - interval 1 day as date)
        end as window_end
    from methods as m
    cross join as_of as a
    where m.has_requirement
),

filing as (
    select
        'FUNDED_RESERVE' as report_id,
        state,
        'PERIOD' as scope,
        'PERIOD' as window_id,
        cast(window_end - interval 1 year + interval 1 day as date) as window_start,
        window_end
    from filing_end
    union all
    select 'FUNDED_RESERVE', state, 'ITD', 'ITD', cast(null as date), window_end
    from filing_end
),

survey_years as (
    select
        m.state,
        y.calendar_year,
        a.last_closed_end
    from methods as m
    cross join as_of as a
    cross join (
        select unnest(range(
            cast(year((select as_of_date from as_of)) as integer) - {{ var('market_survey_years', 5) }} + 1,
            cast(year((select as_of_date from as_of)) as integer) + 1
        )) as calendar_year
    ) as y
    where m.market_survey
),

survey as (
    select
        'MARKET_SURVEY' as report_id,
        state,
        'PERIOD' as scope,
        cast(calendar_year as varchar) as window_id,
        make_date(calendar_year, 1, 1) as window_start,
        cast(least(make_date(calendar_year, 12, 31), last_closed_end) as date) as window_end
    from survey_years
    where last_closed_end >= make_date(calendar_year, 1, 1)
),

windows as (
    select * from filing
    union all
    select * from survey
)

select
    w.report_id,
    w.state,
    w.scope,
    w.window_id,
    case
        when w.report_id = 'MARKET_SURVEY' and w.window_end < make_date(cast(w.window_id as integer), 12, 31)
            then w.window_id || ' YTD (Jan-' || strftime(w.window_end, '%b') || ')'
        when w.scope = 'ITD' then 'Inception to ' || strftime(w.window_end, '%b %d, %Y')
        when w.report_id = 'MARKET_SURVEY' then w.window_id
        else strftime(w.window_start, '%b %d, %Y') || ' to ' || strftime(w.window_end, '%b %d, %Y')
    end as window_label,
    w.window_start,
    w.window_end,
    coalesce(p.is_closed, false) as is_final,
    {{ audit_columns() }}
from windows as w
left join {{ ref('dim_posting_period') }} as p
    on p.posting_period_key = {{ yyyymm('w.window_end') }}
