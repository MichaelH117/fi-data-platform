-- V7 edit 30: in-force profile. "Why each status is or is not counted as in force."
-- Grain: contract status x entered year x expires after the period end (yes / no) x counted (yes / no),
-- at the latest closed posting period end. Counted = the status is in the IN_FORCE status set (seed
-- report_status_sets) AND the date rule of the funded-reserve in-force line holds at the period end
-- (effective on or before it, not expired and not cancelled by then). in_force_set_member says
-- whether the status alone would qualify, so a page can show both halves of the rule.
-- Same product scope as the filing in-force line (report_definitions IN_FORCE, ITD).
with as_of as (
    select cast(max(period_end) as date) as period_end
    from {{ ref('dim_posting_period') }}
    where is_closed
),

line as (
    select category_filter, subcategory_exclude
    from {{ ref('report_definitions') }}
    where report_id = 'FUNDED_RESERVE' and line_item = 'IN_FORCE' and scope = 'ITD'
),

in_force as (
    select status from {{ ref('report_status_sets') }} where status_set_id = 'IN_FORCE'
),

contracts as (
    select
        coalesce(k.status, 'UNKNOWN') as status,
        year(k.entered_date) as entered_year,
        coalesce(k.expiration_date, date '9999-12-31') > a.period_end as expires_after_period_end,
        s.status is not null as in_force_set_member,
        s.status is not null
            and k.effective_date <= a.period_end
            and coalesce(k.expiration_date, date '9999-12-31') > a.period_end
            and coalesce(k.cancel_date, date '9999-12-31') > a.period_end as is_counted,
        a.period_end
    from {{ ref('dim_contract') }} as k
    cross join as_of as a
    cross join line as l
    left join in_force as s on s.status = k.status
    where k.contract_key <> '-1'
      and k.entered_date <= a.period_end
      and (l.category_filter is null or list_contains(string_split(l.category_filter, '|'), k.category_code))
      and (l.subcategory_exclude is null
           or not list_contains(string_split(l.subcategory_exclude, '|'), k.subcategory_code))
)

select
    status,
    entered_year,
    expires_after_period_end,
    in_force_set_member,
    is_counted,
    any_value(period_end) as period_end,
    count(*) as contracts,
    {{ audit_columns() }}
from contracts
group by status, entered_year, expires_after_period_end, in_force_set_member, is_counted
