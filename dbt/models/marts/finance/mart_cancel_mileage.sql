-- V7 edit 36: mileage-near-limit cancels. "Contracts cancelled close to their mileage limit."
-- Grain: one row per effective cancel (fct_cancellation.is_effective_cancel, so a cancel the source
-- later reversed is left out) on a contract with a mileage limit (term_miles > 0).
--   miles_used       odometer at cancel - odometer at sale
--   miles_remaining  term miles - miles used (negative = past the limit)
--   band             seed cancel_mileage_bands; is_near_limit marks the bands the page counts
-- A cancel with no odometer reading at cancel or at sale has no band (mileage_band_id NULL) and is
-- counted on the page as "no odometer", never as 0 miles.
select
    x.cancel_key,
    x.contract_key,
    x.tenant_key,
    x.dealer_key,
    k.contract_number,
    k.category_code,
    k.subcategory_code,
    x.cancel_date,
    x.posting_period_key,
    x.reason_group,
    x.is_admin_correction,
    t.term_months,
    t.term_miles,
    v.odometer as odometer_at_sale,
    x.odometer as odometer_at_cancel,
    x.odometer - v.odometer as miles_used,
    t.term_miles - (x.odometer - v.odometer) as miles_remaining,
    b.band_id as mileage_band_id,
    b.band_label as mileage_band,
    b.sort_order as mileage_band_sort,
    coalesce(b.is_near_limit, false) as is_near_limit,
    x.customer_refund,
    {{ audit_columns() }}
from {{ ref('fct_cancellation') }} as x
inner join {{ ref('dim_contract') }} as k on k.contract_key = x.contract_key
inner join {{ ref('dim_contract_terms') }} as t on t.contract_key = x.contract_key
left join {{ ref('dim_contract_vehicle') }} as v on v.contract_key = x.contract_key
left join {{ ref('cancel_mileage_bands') }} as b
    on t.term_miles - (x.odometer - v.odometer) between b.min_miles_remaining and b.max_miles_remaining
where x.is_effective_cancel and coalesce(t.term_miles, 0) > 0
