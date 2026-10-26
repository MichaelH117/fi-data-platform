-- Rate card (V6 edit 15): price lookup by term, mileage at sale and vehicle class. Grain: one rate row
-- (product plan x coverage level x term months / miles x odometer band x class code x deductible);
-- program_type is the product's insurer program type (edit 3).
-- Built on the existing source rate tables (rate + rate_component), not a second price table.
--   rate          the dealer cost on the card (rate.coverage_cost)
--   fee stack     admin_fee (ADMIN_FEE), program_fee (INSURER_FEE, the insurer-program fee),
--                 premium_tax (PREMIUM_TAX), dealer_pack (COMMISSION)
--   reserve       rate - fees_total; equals claims reserve + profit-share reserve to the cent
--                 (tested in _finance.yml)
-- term_sort / band_sort give the reviewer's order: term first, then odometer band.
-- V6 interim: rate and rate_component are read from staging until core carries a rate dimension
-- (DE Manager). Swap the two refs for that dim then.
with comp as (
    select
        rate_id,
        coalesce(sum(amount) filter (where disbursement_type = 'ADMIN_FEE'), 0) as admin_fee,
        coalesce(sum(amount) filter (where disbursement_type = 'INSURER_FEE'), 0) as program_fee,
        coalesce(sum(amount) filter (where disbursement_type = 'PREMIUM_TAX'), 0) as premium_tax,
        coalesce(sum(amount) filter (where disbursement_type = 'COMMISSION'), 0) as dealer_pack,
        coalesce(sum(amount) filter (where disbursement_type = 'CLAIMS_RESERVE'), 0) as claims_reserve,
        coalesce(sum(amount) filter (where disbursement_type = 'PROFIT_SHARE_RESERVE'), 0) as profit_share_reserve,
        count(*) as components
    from {{ ref('stg_admin_core__rate_component') }}
    where not is_deleted
    group by 1
),

plan as (
    select plan_id, any_value(product_code) as product_code, any_value(product) as product_name,
        any_value(category_code) as category_code, any_value(plan_code) as plan_code,
        any_value(plan_year) as plan_year, any_value(program_type) as program_type
    from {{ ref('dim_product') }}
    where plan_id is not null
    group by 1
)

select
    r.rate_id,
    r.tenant_id as tenant_key,
    p.program_type,
    p.category_code,
    p.product_code,
    p.product_name,
    p.plan_code,
    p.plan_year,
    coalesce(r.coverage_level, 'STANDARD') as coverage_level,
    r.term_months,
    r.term_miles,
    r.odometer_band,
    r.class_code,
    r.deductible_amount,
    round(r.coverage_cost, 2) as rate,
    c.admin_fee,
    c.program_fee,
    c.premium_tax,
    c.dealer_pack,
    round(c.admin_fee + c.program_fee + c.premium_tax + c.dealer_pack, 2) as fees_total,
    round(r.coverage_cost - (c.admin_fee + c.program_fee + c.premium_tax + c.dealer_pack), 2) as reserve,
    c.claims_reserve,
    c.profit_share_reserve,
    r.suggested_retail,
    coalesce(c.components, 0) as components,
    r.term_months * 1000000 + coalesce(r.term_miles, 0) as term_sort,
    coalesce(try_cast(regexp_extract(r.odometer_band, '^([0-9]+)', 1) as integer), -1) as band_sort,
    {{ audit_columns() }}
from {{ ref('stg_admin_core__rate') }} as r
left join comp as c using (rate_id)
left join plan as p using (plan_id)
where not r.is_deleted
