-- Payees of the dealer-cost split and of claim payments.
select
    md5('account_holder|' || cast(account_holder_id as varchar)) as account_holder_key,
    account_holder_id,
    holder_type,
    holder_name,
    state,
    ref_id,
    tenant_id as tenant_key,
    {{ audit_columns() }}
from {{ ref('stg_admin_core__account_holder') }}
where not is_deleted

union all

select '-1', null, 'UNKNOWN', 'Unknown / not split (legacy)', null, null, null, current_timestamp
