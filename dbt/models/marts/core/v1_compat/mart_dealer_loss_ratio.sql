-- DEPRECATED V1 mart (gold.mart_dealer_loss_ratio), rebuilt on the V3 core facts.
-- Grain: dealer_code x calendar month, gap-free from the dealer's first sale month to the as-of
-- month, so trailing windows are simple row frames.
{{ config(schema='gold', materialized='table') }}
with monthly as (
    select
        dealer_code,
        month_start,
        sum(written_premium) as written_premium,
        sum(earned_premium) as earned_premium,
        sum(incurred_claims) as incurred_claims,
        sum(paid_claims) as paid_claims,
        sum(claim_count) as claim_count,
        sum(exposure_years) as exposure_years,
        count(*) filter (where is_in_force_eom) as contracts_in_force
    from {{ ref('v1_fct_contract_monthly') }}
    group by all
),

sales as (
    select dealer_code, cast(date_trunc('month', sale_date) as date) as month_start, count(*) as contracts_sold
    from {{ ref('fct_contract_sale') }}
    group by all
),

firsts as (
    select dealer_code, min(month_start) as first_month from sales group by 1
    union all
    select dealer_code, min(month_start) from monthly group by 1
),

spine as (
    select
        f.dealer_code,
        cast(unnest(generate_series(min(f.first_month), max(date_trunc('month', a.as_of_date)), interval 1 month))
            as date) as month_start
    from firsts as f
    cross join {{ ref('int_as_of_date') }} as a
    group by f.dealer_code
),

joined as (
    select
        sp.dealer_code,
        sp.month_start,
        coalesce(s.contracts_sold, 0) as contracts_sold,
        coalesce(m.contracts_in_force, 0) as contracts_in_force,
        coalesce(m.written_premium, 0) as written_premium,
        coalesce(m.earned_premium, 0) as earned_premium,
        coalesce(m.incurred_claims, 0) as incurred_claims,
        coalesce(m.paid_claims, 0) as paid_claims,
        coalesce(m.claim_count, 0) as claim_count,
        coalesce(m.exposure_years, 0) as exposure_years
    from spine as sp
    left join monthly as m using (dealer_code, month_start)
    left join sales as s using (dealer_code, month_start)
),

windowed as (
    select
        *,
        sum(earned_premium) over w12 as t12_earned_premium,
        sum(incurred_claims) over w12 as t12_incurred_claims,
        sum(claim_count) over w12 as t12_claim_count,
        sum(exposure_years) over w12 as t12_exposure_years,
        sum(earned_premium) over wall as itd_earned_premium,
        sum(incurred_claims) over wall as itd_incurred_claims
    from joined
    window
        w12 as (partition by dealer_code order by month_start rows between 11 preceding and current row),
        wall as (partition by dealer_code order by month_start rows between unbounded preceding and current row)
)

select
    d.dealer_key,
    t.dealer_code,
    d.dealer_name,
    d.state,
    d.reinsurance_company,
    d.dealer_status,
    t.dealer_code = 'UNKNOWN' as is_unknown_dealer,
    t.month_start,
    t.contracts_sold,
    t.contracts_in_force,
    round(t.written_premium, 2) as written_premium,
    round(t.earned_premium, 2) as earned_premium,
    round(t.incurred_claims, 2) as incurred_claims,
    round(t.paid_claims, 2) as paid_claims,
    t.claim_count,
    round(t.exposure_years, 4) as exposure_years,
    {{ safe_divide('t.incurred_claims', 't.earned_premium') }} as loss_ratio,
    round(t.t12_earned_premium, 2) as t12_earned_premium,
    round(t.t12_incurred_claims, 2) as t12_incurred_claims,
    t.t12_claim_count,
    round(t.t12_exposure_years, 4) as t12_exposure_years,
    {{ safe_divide('t.t12_incurred_claims', 't.t12_earned_premium') }} as t12_loss_ratio,
    {{ safe_divide('t.t12_claim_count', 't.t12_exposure_years') }} as t12_claim_frequency,
    {{ safe_divide('t.itd_incurred_claims', 't.itd_earned_premium') }} as itd_loss_ratio,
    coalesce({{ safe_divide('t.t12_incurred_claims', 't.t12_earned_premium') }}
        > {{ var('high_loss_ratio_threshold') }}, false) as is_high_loss_ratio
from windowed as t
left join {{ ref('v1_dim_dealer') }} as d on t.dealer_code = d.dealer_code and d.is_current
