-- Specialty-line contracts (RV with a separate chassis VIN, powersports). Contract grain.
-- Lines and the chassis-VIN rule: seed dealer_specialty_lines. Statuses: seed (SPECIALTY_LINE).
-- VINs stay hashed; *_vin_token is the first 12 hex characters of the hash, enough to match rows.
select
    c.contract_key,
    c.tenant_key,
    c.dealer_key,
    coalesce(d.dealer_number, c.dealer_number) as dealer_number,
    d.dealer_name,
    c.contract_number,
    c.status,
    c.subcategory_code,
    l.line_name,
    pr.product,
    c.entered_date,
    c.effective_date,
    cast(date_trunc('month', c.entered_date) as date) as entered_month,
    left(v.vin_hash, 12) as vin_token,
    left(v.chassis_vin_hash, 12) as chassis_vin_token,
    coalesce(v.has_chassis_vin, false) as has_chassis_vin,
    l.requires_chassis_vin,
    l.requires_chassis_vin and not coalesce(v.has_chassis_vin, false) as is_chassis_vin_missing,
    v.vehicle_make,
    v.vehicle_model,
    v.vehicle_year,
    v.new_used,
    f.purchase_price,
    f.coverage_cost,
    f.retail_price,
    coalesce(cs.claim_count, 0) as claim_count,
    coalesce(cs.paid_to_date, 0) as claims_paid,
    {{ audit_columns() }}
from {{ ref('dim_contract') }} as c
inner join {{ ref('dealer_specialty_lines') }} as l on l.subcategory_code = c.subcategory_code
inner join {{ ref('dealer_report_status_sets') }} as s
    on s.report_id = 'SPECIALTY_LINE' and s.status = c.status
left join {{ ref('dim_dealer') }} as d on d.dealer_key = c.dealer_key
left join {{ ref('dim_product') }} as pr on pr.product_key = c.product_key
left join {{ ref('dim_contract_vehicle') }} as v on v.contract_key = c.contract_key
left join {{ ref('dim_contract_finance') }} as f on f.contract_key = c.contract_key
left join {{ ref('dim_contract_claim_summary') }} as cs on cs.contract_key = c.contract_key
where c.contract_key <> '-1'
