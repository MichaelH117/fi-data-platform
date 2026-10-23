-- DEPRECATED V1 mart (gold.mart_claims_trend), derived from the one claims fact.
-- Grain: loss month x coverage tier x repair category.
{{ config(schema='gold', materialized='table') }}
select
    cast(date_trunc('month', c.loss_date) as date) as loss_month,
    p.coverage_tier,
    c.repair_category,
    count(*) as claims_reported,
    count(*) filter (where c.claim_status = 'paid') as claims_paid,
    count(*) filter (where c.claim_status = 'denied') as claims_denied,
    count(*) filter (where c.claim_status = 'open') as claims_open,
    {{ safe_divide("count(*) filter (where c.claim_status = 'denied')", 'count(*)') }} as denial_rate,
    round(sum(c.claim_amount_requested), 2) as amount_requested,
    round(sum(c.paid_amount), 2) as paid_amount,
    round(sum(c.incurred_amount), 2) as incurred_amount,
    round({{ safe_divide('sum(c.paid_amount)', "count(*) filter (where c.claim_status = 'paid')") }}, 2)
        as avg_severity,
    round(avg(c.report_lag_days), 1) as avg_report_lag_days,
    count(*) filter (where c.is_late_reported) as claims_late_reported
from {{ ref('fct_claim') }} as c
left join {{ ref('v1_dim_product') }} as p on c.product_code = p.product_code
group by all
