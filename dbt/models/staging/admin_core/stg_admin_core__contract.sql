-- Admin Core contracts: latest version per contract_id. Customer email, zip, VINs and phone are
-- hashed or dropped here. The fake customer name is kept ONLY in staging (contains_pii), for the
-- pending-contracts mart; intermediate and core models never select it. phone_hash = sha256 of the
-- phone digits, the same rule the call-center CRM uses (bridges calls that carry no contract number).
with latest as (
    {{ stg_admin_core('contract', 'contract_id') }}
)

select
    * exclude (customer_email, customer_zip, vin, chassis_vin, customer_phone) replace (
        -- sentinel placeholder dates -> NULL, once (macros/foundations.sql)
        {{ sentinel_to_null('entered_date') }} as entered_date,
        {{ sentinel_to_null('effective_date') }} as effective_date,
        {{ sentinel_to_null('expiration_date') }} as expiration_date
    ),
    {{ any_sentinel_date(['entered_date', 'effective_date', 'expiration_date']) }} as is_sentinel_date,
    case when regexp_replace(coalesce(customer_phone, ''), '[^0-9]', '', 'g') <> ''
         then sha256(regexp_replace(customer_phone, '[^0-9]', '', 'g')) end as phone_hash,
    upper(trim(contract_number)) as contract_number_clean,
    {{ hash_pii('customer_email') }} as customer_key,
    left(trim(customer_zip), 3) as customer_zip3,
    {{ hash_pii('upper(vin)') }} as vin_hash,
    {{ is_valid_vin('vin') }} as is_valid_vin,
    {{ hash_pii('upper(chassis_vin)') }} as chassis_vin_hash,
    chassis_vin is not null and trim(chassis_vin) <> '' as has_chassis_vin
from latest
