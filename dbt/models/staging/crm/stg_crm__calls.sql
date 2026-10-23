-- Call-center CRM contacts: typed, minus every quarantined row (anti-join on _quarantine), one row
-- per call_id (a DELTA that resends a call keeps the latest delivery). The CRM knows the customer
-- only by contract_number and/or phone_hash (already hashed by the CRM; no raw phone arrives).
with accepted as (
    {{ intake_accepted('crm', 'calls') }}
),

latest as (
    select *
    from accepted
    qualify row_number() over (
        partition by call_id order by _feed_date desc, _source_file desc, _row_number desc
    ) = 1
)

select
    trim(call_id) as call_id,
    try_cast(call_at as timestamp) as call_at,
    cast(try_cast(call_at as timestamp) as date) as call_date,
    upper(trim(channel)) as channel,
    upper(trim(queue)) as queue,
    upper(trim(reason)) as reason,
    nullif(upper(trim(disposition)), '') as disposition,
    upper(trim(tenant_code)) as tenant_code,
    nullif(upper(trim(contract_number)), '') as contract_number,
    nullif(lower(trim(phone_hash)), '') as phone_hash,
    nullif(trim(complaint_category), '') as complaint_category,
    coalesce(try_cast(is_regulator_referral as integer), 0) = 1 as is_regulator_referral,
    try_cast(handle_seconds as integer) as handle_seconds,
    _feed_date as feed_date,
    _source_file,
    _row_number,
    _source_system,
    _loaded_at
from latest
