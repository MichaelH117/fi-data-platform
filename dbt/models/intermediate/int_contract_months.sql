{{ config(materialized='table') }}
-- Contract x calendar-month earning spine, from the effective month to the month earning stops.
--   stop_date = least(expiration_date, effective (non-reinstated) cancel date, void date, as_of_date)
--   pro-rata  E(d) = clamp(days(effective -> d) / term_days, 0, 1)
--   rule of 78 E(d) = 1 - (n - k)(n - k + 1) / (n (n + 1)),  n = term months, k = E_pro_rata(d) * n
-- earned_pct is the share of the contract earned IN the month (method: var earning_method).
--
-- Earning follows the STATUS TIMELINE, not the current status (docs/V3_DESIGN.md 2.5: earned facts
-- are never restated after close):
--   activated_at  first time the contract entered an earning status (anything but PENDING, VOIDED,
--                 DENIED) in int_status_history_backfill or int_contract_versions. A contract that
--                 never activated earns nothing.
--   earned_month_key   the true month being earned (yyyymm of month_start).
--   posting_period_key = posting_period_key rule (macro) with the activation date as the recorded
--                 date, so a month that was already closed when the contract activated (late
--                 remittance, PENDING -> INFORCE) is caught up in the first period open on the
--                 activation date instead of back-filling the closed period.
--   VOID / DENY after activation: earning stops at the void date and one VOID_REVERSAL row reverses
--                 everything earned, posted to the first period open on the void date. Rows already
--                 reported are never deleted.
-- row_type: EARN (one row per contract x month) | VOID_REVERSAL (at most one per contract).
{% set non_earning = "('PENDING', 'VOIDED', 'DENIED')" %}
{% set void_statuses = "('VOIDED', 'DENIED')" %}

with timeline as (
    select entity_key as contract_key, status, valid_from as changed_at
    from {{ ref('int_status_history_backfill') }}
    where entity_type = 'contract'
    union all
    select contract_key, status, valid_from
    from {{ ref('int_contract_versions') }}
    where not is_deleted
),

activation as (
    select contract_key, min(changed_at) as activated_at
    from timeline
    where status not in {{ non_earning }} and changed_at is not null
    group by 1
),

voids as (
    -- only a contract whose CURRENT status is a void/deny is reversed; the void time is the first
    -- void/deny after activation.
    select t.contract_key, min(t.changed_at) as voided_at
    from timeline as t
    inner join activation as a using (contract_key)
    inner join {{ ref('int_contracts_conformed') }} as c using (contract_key)
    where t.status in {{ void_statuses }} and t.changed_at >= a.activated_at
      and c.status in {{ void_statuses }}
    group by 1
),

stops as (
    select
        c.contract_key,
        c.effective_date,
        c.expiration_date,
        c.term_days,
        greatest(coalesce(c.term_months, 0), 1) as n,
        x.cancel_date,
        act.activated_at,
        v.voided_at,
        least(
            c.expiration_date,
            coalesce(x.cancel_date, c.expiration_date),
            coalesce(cast(v.voided_at as date), c.expiration_date),
            a.as_of_date
        ) as stop_date
    from {{ ref('int_contracts_conformed') }} as c
    cross join {{ ref('int_as_of_date') }} as a
    inner join activation as act using (contract_key)
    left join voids as v using (contract_key)
    left join {{ ref('int_cancellations_conformed') }} as x
        on x.contract_key = c.contract_key and x.is_effective_cancel
    where c.effective_date <= a.as_of_date
      and c.term_days > 0
      and cast(act.activated_at as date) <= a.as_of_date
),

spine as (
    select
        stops.*,
        cast(unnest(generate_series(
            date_trunc('month', effective_date), date_trunc('month', greatest(stop_date, effective_date)), interval 1 month
        )) as date) as month_start
    from stops
),

periods as (
    select
        *,
        greatest(month_start, effective_date) as period_start,
        greatest(least(cast(month_start + interval 1 month as date), stop_date), effective_date) as period_end
    from spine
),

fractions as (
    select
        *,
        greatest(0, least(1, datediff('day', effective_date, period_start) / term_days)) as p_start,
        greatest(0, least(1, datediff('day', effective_date, period_end) / term_days)) as p_end
    from periods
),

earned as (
    select
        *,
        1 - (n - p_start * n) * (n - p_start * n + 1) / (n * (n + 1.0)) as r_start,
        1 - (n - p_end * n) * (n - p_end * n + 1) / (n * (n + 1.0)) as r_end
    from fractions
),

earn_rows as (
    select
        e.contract_key,
        'EARN' as row_type,
        e.month_start,
        {{ yyyymm('e.month_start') }} as earned_month_key,
        {{ posting_period_key('e.month_start', 'pp') }} as posting_period_key,
        e.activated_at,
        e.voided_at,
        e.period_start,
        e.period_end,
        datediff('day', e.period_start, e.period_end) as earned_days,
        datediff('day', e.period_start, e.period_end) / 365.25 as exposure_years,
        e.p_end - e.p_start as earned_pct_pro_rata,
        e.r_end - e.r_start as earned_pct_r78,
        {% if var('earning_method') == 'r78' %}e.r_end - e.r_start{% else %}e.p_end - e.p_start{% endif %} as earned_pct,
        {% if var('earning_method') == 'r78' %}e.r_end{% else %}e.p_end{% endif %} as earned_pct_cumulative,
        e.period_end < e.expiration_date and (e.cancel_date is null or e.cancel_date > e.period_end)
            and e.voided_at is null as is_in_force_eom
    from earned as e
    {{ posting_period_join('e.activated_at', 'pp') }}
),

void_rows as (
    select
        r.contract_key,
        'VOID_REVERSAL' as row_type,
        cast(date_trunc('month', r.voided_at) as date) as month_start,
        {{ yyyymm('r.voided_at') }} as earned_month_key,
        {{ posting_period_key('r.voided_at', 'pv') }} as posting_period_key,
        any_value(r.activated_at) as activated_at,
        r.voided_at,
        cast(r.voided_at as date) as period_start,
        cast(r.voided_at as date) as period_end,
        -sum(r.earned_days) as earned_days,
        -sum(r.exposure_years) as exposure_years,
        -sum(r.earned_pct_pro_rata) as earned_pct_pro_rata,
        -sum(r.earned_pct_r78) as earned_pct_r78,
        -sum(r.earned_pct) as earned_pct,
        0.0 as earned_pct_cumulative,
        false as is_in_force_eom
    from earn_rows as r
    {{ posting_period_join('r.voided_at', 'pv') }}
    where r.voided_at is not null
    group by r.contract_key, r.voided_at, pv.first_open_period_key
)

select * from earn_rows
union all
select * from void_rows
