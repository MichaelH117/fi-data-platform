-- Vehicle on the contract (one row per reportable contract). VINs exist only as hashes.
-- Specialty-line (RV) coach and chassis attributes come from the Admin Core custom fields (EAV),
-- pivoted to typed columns once in stg_admin_core__contract_custom_field; NULL when the contract
-- has no custom fields.
with c as (
    select c.*
    from {{ ref('int_contracts_conformed') }} as c
    inner join {{ ref('int_reportable_scope') }} as s using (contract_key)
    where s.is_reportable
),

custom as (
    select
        {{ contract_key("'admin_core'", 'contract_id') }} as contract_key,
        coach_make,
        coach_model,
        chassis_make,
        chassis_vin_hash as custom_chassis_vin_hash,
        model_year as custom_model_year,
        invalid_value_count
    from {{ ref('stg_admin_core__contract_custom_field') }}
)

select
    c.contract_key,
    c.vin_hash,
    c.is_valid_vin,
    c.chassis_vin_hash,
    c.has_chassis_vin,
    c.vehicle_year,
    c.vehicle_make,
    c.vehicle_model,
    c.fuel_type,
    c.cylinders,
    c.drive_type,
    c.is_commercial,
    c.title_brand,
    c.new_used,
    c.odometer,
    c.odometer_band,
    x.coach_make,
    x.coach_model,
    x.chassis_make,
    x.custom_chassis_vin_hash,
    x.custom_model_year,
    x.contract_key is not null as has_custom_fields,
    coalesce(x.invalid_value_count, 0) as custom_field_invalid_count,
    {{ audit_columns() }}
from c
left join custom as x using (contract_key)
