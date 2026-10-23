-- Dealer early-claim watch of record: actual vs expected early-claim CONTRACTS.
-- Grain: tenant x dealer x month (month_key = yyyymm), one window (seed scorecard_early_claim_ae_window_days).
-- Each month is evaluated on its reference date: the month end, or the as-of date for the current month,
-- so the as-of month row is the same test as the Actuarial page dealer list
-- (actuarial/emergence.py early_claim_by_dealer, table actuarial.early_claim_dealer; tests/test_dealer.py
-- checks the two agree dealer by dealer).
--   exposed    contract effective for the whole window by the reference date
--   early      first loss (denied claims INCLUDED: an early claim is a signal even when not paid) within
--              the window of the effective date
--   expected   sum over the dealer's exposed contracts of the portfolio early rate of the contract's own
--              product segment (category x subcategory x new/used), so product mix alone never flags a dealer
--   z score    Poisson (early - expected) / sqrt(expected)
--   flagged    exposed >= scorecard_early_claim_min_exposed, A/E >= scorecard_early_claim_min_ae,
--              z >= scorecard_early_claim_min_z
-- Claims are read as known today, so a past month's row uses claims reported after that month end
-- (only losses inside the window count, and those happened before the reference date).
-- Contract scope matches actuarial/data.py: term_months > 0, effective date known, not the unknown member.
with rules as (
    select
        max(case when rule_id = 'scorecard_early_claim_ae_window_days' then cast(rule_value as integer) end)
            as window_days,
        max(case when rule_id = 'scorecard_early_claim_min_exposed' then cast(rule_value as integer) end)
            as min_exposed,
        max(case when rule_id = 'scorecard_early_claim_min_ae' then cast(rule_value as double) end) as min_ae,
        max(case when rule_id = 'scorecard_early_claim_min_z' then cast(rule_value as double) end) as min_z
    from {{ ref('dealer_claim_kpi_rules') }}
),

as_of as (
    select cast(as_of_date as date) as as_of_date from {{ ref('dim_reporting_date') }}
),

contracts as (
    select
        c.contract_key,
        c.tenant_key,
        c.dealer_key,
        c.dealer_number,
        coalesce(c.category_code, 'UNKNOWN') as category_code,
        coalesce(c.subcategory_code, 'UNKNOWN') as subcategory_code,
        coalesce(upper(v.new_used), 'UNKNOWN') as new_used,
        cast(c.effective_date as date) as effective_date
    from {{ ref('dim_contract') }} as c
    inner join {{ ref('dim_contract_terms') }} as t using (contract_key)
    left join {{ ref('dim_contract_vehicle') }} as v using (contract_key)
    cross join as_of as a
    where c.contract_key <> '-1' and t.term_months > 0 and c.effective_date is not null
      and cast(c.effective_date as date) <= a.as_of_date
),

first_claim as (
    select k.contract_key, min(datediff('day', c.effective_date, k.loss_date)) as first_claim_days
    from {{ ref('dim_claim') }} as k
    inner join contracts as c on c.contract_key = k.contract_key
    cross join as_of as a
    where k.loss_date is not null and k.loss_date <= a.as_of_date and k.loss_date >= c.effective_date
    group by 1
),

months as (
    select
        cast(m.month_start as date) as month_start,
        least(last_day(cast(m.month_start as date)), a.as_of_date) as reference_date
    from as_of as a
    cross join rules as r
    cross join (select min(effective_date) as first_effective from contracts) as f
    cross join unnest(generate_series(
        cast(date_trunc('month', f.first_effective + r.window_days) as date),
        cast(date_trunc('month', a.as_of_date) as date),
        interval 1 month
    )) as m (month_start)
),

exposed as (
    select
        m.month_start,
        m.reference_date,
        c.*,
        cast(coalesce(fc.first_claim_days <= r.window_days, false) as integer) as is_early
    from months as m
    cross join rules as r
    inner join contracts as c on datediff('day', c.effective_date, m.reference_date) >= r.window_days
    left join first_claim as fc on fc.contract_key = c.contract_key
),

segment_rate as (
    select month_start, category_code, subcategory_code, new_used, avg(is_early) as segment_early_rate
    from exposed
    group by all
),

by_dealer as (
    select
        e.tenant_key,
        e.dealer_key,
        e.dealer_number,
        e.month_start,
        e.reference_date,
        count(*) as contracts_exposed,
        sum(e.is_early) as early_claim_contracts,
        sum(s.segment_early_rate) as expected_early_claims
    from exposed as e
    inner join segment_rate as s using (month_start, category_code, subcategory_code, new_used)
    where e.dealer_number is not null
    group by all
),

scored as (
    select
        d.*,
        r.window_days,
        d.early_claim_contracts / d.contracts_exposed as early_claim_incidence,
        case when d.expected_early_claims > 0 then d.early_claim_contracts / d.expected_early_claims end
            as actual_to_expected,
        case when d.expected_early_claims > 0
             then (d.early_claim_contracts - d.expected_early_claims) / sqrt(d.expected_early_claims) end
            as z_score,
        r.min_exposed,
        r.min_ae,
        r.min_z
    from by_dealer as d
    cross join rules as r
)

select
    tenant_key,
    dealer_key,
    dealer_number,
    month_start,
    {{ yyyymm('month_start') }} as month_key,
    reference_date,
    window_days,
    contracts_exposed,
    early_claim_contracts,
    expected_early_claims,
    early_claim_incidence,
    actual_to_expected,
    z_score,
    coalesce(contracts_exposed >= min_exposed and actual_to_expected >= min_ae and z_score >= min_z, false)
        as is_flagged,
    {{ audit_columns() }}
from scored
