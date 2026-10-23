-- Legacy claims: latest version per normalized claim number ('0000123' -> '123').
-- Legacy stores claim money NEGATIVE; staging flips the sign so paid is always >= 0.
with cleaned as (
    select *, {{ normalize_claim_number('claim_no') }} as claim_number
    from {{ source('legacy_admin', 'claims') }}
),

latest as (
    {{ latest_version('cleaned', 'claim_number') }}
)

select
    claim_number,
    claim_no as claim_no_raw,
    legacy_contract_id,
    loss_date,
    coalesce(reported_date, loss_date) as reported_date,
    paid_date,
    odometer_at_loss,
    lower(trim(repair_category)) as repair_category,
    abs(amount_requested) as amount_requested,
    abs(amount_paid) as amount_paid,
    (amount_requested < 0 or amount_paid < 0) is true as is_amount_sign_fixed,
    lower(trim(status)) as claim_status,
    created_at,
    updated_at,
    is_deleted,
    _source_system,
    _loaded_at
from latest
where claim_number is not null
