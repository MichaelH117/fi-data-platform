-- DEPRECATED V1 shape (gold.fct_claim): one row per valid claim, DERIVED from the one claims fact
-- (core.fct_claim_payment_line) and core.dim_claim. Valid = has a contract and the loss is on or
-- after the sale. claim_status is the V1 vocabulary (paid / open / denied).
-- incurred_amount = paid + outstanding case reserve. Kept (by name) so metrics.yml still parses.
{{ config(schema='gold', materialized='table') }}
with money as (
    select
        claim_key,
        sum(paid_amount) as paid,
        sum(paid_amount + pending_amount) as incurred,
        max(posted_at) filter (where paid_amount > 0) as paid_at
    from {{ ref('fct_claim_payment_line') }}
    group by 1
)

select
    c.claim_key,
    c.claim_key as claim_id,
    s.contract_key,
    s.contract_id,
    s.dealer_key,
    s.dealer_code,
    s.product_key,
    s.product_code,
    c.loss_date,
    c.loss_date_key,
    c.reported_date,
    cast(m.paid_at as date) as paid_date,
    {{ date_key('m.paid_at') }} as paid_date_key,
    datediff('day', s.sale_date, c.loss_date) as days_since_sale,
    c.report_lag_days,
    c.is_late_reported,
    c.repair_category,
    c.odometer_at_loss,
    c.status_group as claim_status,
    c.requested_amount as claim_amount_requested,
    coalesce(m.paid, 0) as claim_amount_paid,
    coalesce(m.paid, 0) as paid_amount,
    coalesce(m.incurred, 0) as incurred_amount,
    c.is_amount_sign_fixed,
    c.source_system
from {{ ref('dim_claim') }} as c
inner join {{ ref('fct_contract_sale') }} as s on s.contract_key = c.contract_key
left join money as m using (claim_key)
where c.loss_date >= s.sale_date
