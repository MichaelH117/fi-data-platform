-- DEPRECATED V1 shape (gold.fct_contract_sale), rebuilt on the V3 core. contract_id is the V3
-- contract_key; product_code is the V1 code (subcategory-term). Build new work on core.dim_contract
-- and the split contract dimensions instead. Kept (by name) so dbt/models/metrics.yml still parses.
{{ config(schema='gold', materialized='table') }}
select
    k.contract_key,
    k.contract_key as contract_id,
    k.dealer_key,
    k.dealer_number as dealer_code,
    k.agent_key,
    k.agent_key as agent_id,
    md5('v1_product|' || k.subcategory_code || '-' || cast(t.term_months as varchar)) as product_key,
    k.subcategory_code || '-' || cast(t.term_months as varchar) as product_code,
    k.customer_key,
    v.vin_hash,
    k.entered_date as sale_date,
    k.entered_date_key as sale_date_key,
    k.effective_date,
    k.effective_date_key,
    k.expiration_date,
    t.term_months,
    t.term_miles,
    t.term_days,
    v.new_used,
    v.odometer as odometer_at_sale,
    f.retail_price,
    f.coverage_cost as dealer_cost,
    coalesce(f.admin_fee, 0) as admin_fee,
    coalesce(f.reserve_amount, 0) as reserve_amount,
    coalesce(f.commission, 0) as commission,
    f.dealer_markup,
    lower(k.status) as contract_status,
    k.cancel_date,
    k.is_cancelled,
    v.is_valid_vin,
    k.is_dealer_code_missing,
    f.is_amount_sign_fixed,
    k.is_recovered_from_quarantine,
    k.source_system
from {{ ref('dim_contract') }} as k
inner join {{ ref('dim_contract_terms') }} as t using (contract_key)
inner join {{ ref('dim_contract_finance') }} as f using (contract_key)
inner join {{ ref('dim_contract_vehicle') }} as v using (contract_key)
where k.status not in ('VOIDED', 'DENIED')
