-- Vehicle on the contract (one row per reportable contract). VINs exist only as hashes.
with c as (
    select c.*
    from {{ ref('int_contracts_conformed') }} as c
    inner join {{ ref('int_reportable_scope') }} as s using (contract_key)
    where s.is_reportable
)

select
    contract_key,
    vin_hash,
    is_valid_vin,
    chassis_vin_hash,
    has_chassis_vin,
    vehicle_year,
    vehicle_make,
    vehicle_model,
    fuel_type,
    cylinders,
    drive_type,
    is_commercial,
    title_brand,
    new_used,
    odometer,
    odometer_band,
    {{ audit_columns() }}
from c
