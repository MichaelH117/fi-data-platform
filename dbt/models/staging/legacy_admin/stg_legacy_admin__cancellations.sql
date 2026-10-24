-- Legacy cancellations: latest version per cancellation_id, refund sign-corrected.
with latest as (
    {{ latest_version(source('legacy_admin', 'cancellations'), 'cancellation_id') }}
)

select
    cancellation_id,
    legacy_contract_id,
    {{ sentinel_to_null('cancel_date') }} as cancel_date,
    {{ is_sentinel_date('cancel_date') }} as is_sentinel_date,
    trim(cancel_reason) as cancel_reason,
    upper(trim(cancel_method)) as cancel_method,
    odometer_at_cancel,
    abs(refund_amount) as refund_amount,
    abs(cancel_fee) as cancel_fee,
    (refund_amount < 0 or cancel_fee < 0) is true as is_amount_sign_fixed,
    created_at,
    updated_at,
    is_deleted,
    _source_system,
    _loaded_at
from latest
