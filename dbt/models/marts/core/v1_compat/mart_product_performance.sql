-- DEPRECATED V1 mart (gold.mart_product_performance), rebuilt on the V3 core facts.
-- Grain: V1 product_code (subcategory-term) x new_used, inception to date.
{{ config(schema='gold', materialized='table') }}
with sales as (
    select
        product_code,
        new_used,
        count(*) as contracts_sold,
        count(*) filter (where is_cancelled) as contracts_cancelled,
        sum(reserve_amount) as written_premium,
        avg(retail_price) as avg_retail_price,
        avg(dealer_markup) as avg_dealer_markup
    from {{ ref('fct_contract_sale') }}
    group by all
),

earned as (
    select
        product_code,
        new_used,
        sum(earned_premium) as earned_premium,
        sum(incurred_claims) as incurred_claims,
        sum(paid_claims) as paid_claims,
        sum(claim_count) as claim_count,
        sum(exposure_years) as exposure_years
    from {{ ref('v1_fct_contract_monthly') }}
    group by all
),

paid as (
    select product_code, s.new_used, count(*) as paid_claim_count
    from {{ ref('fct_claim') }} as c
    inner join {{ ref('fct_contract_sale') }} as s using (contract_key, product_code)
    where c.claim_status = 'paid'
    group by all
)

select
    p.product_key,
    s.product_code,
    p.coverage_tier,
    p.tier_rank,
    p.term_months,
    s.new_used,
    s.contracts_sold,
    s.contracts_cancelled,
    {{ safe_divide('s.contracts_cancelled', 's.contracts_sold') }} as cancel_rate,
    round(s.written_premium, 2) as written_premium,
    round(e.earned_premium, 2) as earned_premium,
    round(e.incurred_claims, 2) as incurred_claims,
    round(e.paid_claims, 2) as paid_claims,
    e.claim_count,
    coalesce(pd.paid_claim_count, 0) as paid_claim_count,
    round(e.exposure_years, 2) as exposure_years,
    {{ safe_divide('e.incurred_claims', 'e.earned_premium') }} as loss_ratio,
    {{ safe_divide('e.claim_count', 'e.exposure_years') }} as claim_frequency,
    {{ safe_divide('e.paid_claims', 'pd.paid_claim_count') }} as claim_severity,
    {{ safe_divide('e.incurred_claims', 'e.exposure_years') }} as pure_premium,
    round(s.avg_retail_price, 2) as avg_retail_price,
    round(s.avg_dealer_markup, 2) as avg_dealer_markup
from sales as s
left join earned as e using (product_code, new_used)
left join paid as pd using (product_code, new_used)
left join {{ ref('v1_dim_product') }} as p on s.product_code = p.product_code
