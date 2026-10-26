-- Pending contracts the dealer still has to finish (paperwork / remittance). Contract grain.
-- contains_pii: the customer names are FAKE (synthetic generator) and are the one documented
-- exception to "no PII past staging" (docs/V3_DESIGN.md 2.4). VINs stay hashed (vin_token).
-- V6 edit 10 (packet formatting): names and addresses in Proper Case (macro proper_case + seed
-- proper_case_exceptions); the co-buyer comes from the e-contracting feed (stg_network_feed__deals,
-- joined on deal_id; the admin contract never carries it), so it is carried over, not retyped.
-- Customer street address and lienholder address have no source yet: not invented here.
with pending as (
    select c.*
    from {{ ref('dim_contract') }} as c
    inner join {{ ref('dealer_report_status_sets') }} as s
        on s.report_id = 'DEALER_PENDING' and s.status = c.status
    where c.contract_key <> '-1'
),

customer as (
    select b.contract_key, s.customer_first_name, s.customer_last_name, s.customer_state
    from {{ ref('int_contract_key_bridge') }} as b
    inner join {{ ref('stg_admin_core__contract') }} as s on s.contract_id = b.admin_contract_id
    where b.source_system = 'admin_core'
    union all
    select b.contract_key, s.customer_first_name, s.customer_last_name, s.customer_state
    from {{ ref('int_contract_key_bridge') }} as b
    inner join {{ ref('stg_legacy_admin__contracts') }} as s on s.legacy_contract_id = b.legacy_contract_id
    where b.source_system = 'legacy_admin'
),

cobuyer as (
    select deal_id, cobuyer_name, cobuyer_address_line1, cobuyer_city, cobuyer_state, cobuyer_zip
    from {{ ref('stg_network_feed__deals') }}
    where cobuyer_name is not null
),

pc as ({{ proper_case_exceptions_map() }}),

as_of as (select cast(as_of_date as date) as as_of_date from {{ ref('dim_reporting_date') }})

select
    p.contract_key,
    p.tenant_key,
    t.tenant_code,
    t.reportable_name as administrator_name,
    p.dealer_key,
    coalesce(d.dealer_number, p.dealer_number) as dealer_number,
    d.dealer_name,
    p.contract_number,
    {{ proper_case('cu.customer_first_name') }} as customer_first_name,
    {{ proper_case('cu.customer_last_name') }} as customer_last_name,
    upper(cu.customer_state) as customer_state,
    {{ proper_case('cb.cobuyer_name') }} as cobuyer_name,
    {{ proper_case('cb.cobuyer_address_line1') }} as cobuyer_address_line1,
    {{ proper_case('cb.cobuyer_city') }} as cobuyer_city,
    cb.cobuyer_state,
    cb.cobuyer_zip,
    cb.deal_id is not null as has_cobuyer,
    left(v.vin_hash, 12) as vin_token,
    p.product_code,
    pr.product,
    pr.plan,
    p.subcategory_code,
    p.entered_date as date_sold,
    p.effective_date,
    {{ proper_case('f.lienholder_name') }} as lienholder_name,
    {{ proper_case('o.obligor_name') }} as provider_name,
    f.retail_price,
    f.coverage_cost,
    datediff('day', p.entered_date, a.as_of_date) as days_pending,
    b.band_label as pending_age_band,
    b.band_order as pending_age_band_order,
    a.as_of_date,
    {{ audit_columns() }}
from pending as p
cross join as_of as a
cross join pc
left join cobuyer as cb on cb.deal_id = p.deal_id
left join customer as cu on cu.contract_key = p.contract_key
left join {{ ref('dim_tenant') }} as t on t.tenant_key = p.tenant_key
left join {{ ref('dim_dealer') }} as d on d.dealer_key = p.dealer_key
left join {{ ref('dim_product') }} as pr on pr.product_key = p.product_key
left join {{ ref('dim_contract_vehicle') }} as v on v.contract_key = p.contract_key
left join {{ ref('dim_contract_finance') }} as f on f.contract_key = p.contract_key
left join {{ ref('dim_obligor') }} as o on o.obligor_key = p.current_obligor_key
left join {{ ref('dealer_age_bands') }} as b
    on b.band_set = 'PENDING'
    and greatest(datediff('day', p.entered_date, a.as_of_date), 0) between b.min_days and b.max_days
