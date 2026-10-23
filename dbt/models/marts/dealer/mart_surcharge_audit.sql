-- Surcharge audit: one row per contract x surcharge code that is EXPECTED (by the vehicle rules in
-- seed dealer_surcharge_rules) or CHARGED (on the contract). finding:
--   OK               expected and charged at the rated amount
--   MISSING          expected but not charged (revenue leak; the dealer must re-rate)
--   NOT_EXPECTED     charged but the vehicle does not qualify (customer over-charged)
--   AMOUNT_MISMATCH  expected and charged, but not at the rated amount
-- Scope: Admin Core contracts (the legacy system did not carry vehicle attributes) in a surcharge-
-- rated subcategory (seed dealer_surcharge_subcategories), statuses per seed (SURCHARGE_AUDIT).
with scope as (
    select c.contract_key, c.source_contract_id, c.contract_number, c.status, c.entered_date,
        c.dealer_key, c.dealer_number, c.tenant_key, c.subcategory_code
    from {{ ref('dim_contract') }} as c
    inner join {{ ref('dealer_report_status_sets') }} as s
        on s.report_id = 'SURCHARGE_AUDIT' and s.status = c.status
    inner join {{ ref('dealer_surcharge_subcategories') }} as sc on sc.subcategory_code = c.subcategory_code
    where c.contract_key <> '-1' and c.source_system = 'admin_core'
),

attrs as (
    select s.contract_key, 'fuel_type' as vehicle_attribute, v.fuel_type as attribute_value
    from scope as s inner join {{ ref('dim_contract_vehicle') }} as v using (contract_key)
    union all
    select s.contract_key, 'drive_type', v.drive_type
    from scope as s inner join {{ ref('dim_contract_vehicle') }} as v using (contract_key)
    union all
    select s.contract_key, 'is_commercial', case when v.is_commercial then 'true' else 'false' end
    from scope as s inner join {{ ref('dim_contract_vehicle') }} as v using (contract_key)
    union all
    select s.contract_key, 'has_title_brand', case when v.title_brand is not null then 'true' else 'false' end
    from scope as s inner join {{ ref('dim_contract_vehicle') }} as v using (contract_key)
    union all
    select s.contract_key, 'class_cylinders', a.class_code || '-' || cast(v.cylinders as varchar)
    from scope as s
    inner join {{ ref('dim_contract_vehicle') }} as v using (contract_key)
    inner join {{ ref('dim_contract_actuarial_class') }} as a using (contract_key)
),

expected as (
    select a.contract_key, r.surcharge_code,
        max(r.expected_amount) as expected_amount,
        string_agg(distinct r.rule_description, '; ') as expected_reason
    from attrs as a
    inner join {{ ref('dealer_surcharge_rules') }} as r
        on r.vehicle_attribute = a.vehicle_attribute and r.match_value = a.attribute_value
    group by all
),

charged as (
    select s.contract_key, cs.surcharge_code, sum(cs.amount) as charged_amount
    from scope as s
    inner join {{ ref('stg_admin_core__contract_surcharge') }} as cs
        on cast(cs.contract_id as varchar) = s.source_contract_id
    where not coalesce(cs.is_deleted, false)
    group by all
),

audit as (
    select
        coalesce(e.contract_key, c.contract_key) as contract_key,
        coalesce(e.surcharge_code, c.surcharge_code) as surcharge_code,
        e.contract_key is not null as is_expected,
        c.contract_key is not null as is_charged,
        e.expected_reason,
        coalesce(e.expected_amount, 0) as expected_amount,
        coalesce(c.charged_amount, 0) as charged_amount
    from expected as e
    full outer join charged as c using (contract_key, surcharge_code)
)

select
    a.contract_key,
    s.tenant_key,
    s.dealer_key,
    coalesce(d.dealer_number, s.dealer_number) as dealer_number,
    d.dealer_name,
    s.contract_number,
    s.status as contract_status,
    s.subcategory_code,
    s.entered_date as contract_date,
    left(v.vin_hash, 12) as vin_token,
    v.fuel_type,
    v.drive_type,
    v.is_commercial,
    v.title_brand,
    a.surcharge_code,
    a.is_expected,
    a.is_charged,
    case
        when a.is_expected and not a.is_charged then 'MISSING'
        when a.is_charged and not a.is_expected then 'NOT_EXPECTED'
        when abs(a.charged_amount - a.expected_amount) > 0.005 then 'AMOUNT_MISMATCH'
        else 'OK'
    end as finding,
    a.expected_reason,
    round(a.expected_amount, 2) as expected_amount,
    round(a.charged_amount, 2) as charged_amount,
    round(a.charged_amount - a.expected_amount, 2) as amount_variance,
    {{ audit_columns() }}
from audit as a
inner join scope as s on s.contract_key = a.contract_key
left join {{ ref('dim_dealer') }} as d on d.dealer_key = s.dealer_key
left join {{ ref('dim_contract_vehicle') }} as v on v.contract_key = a.contract_key
