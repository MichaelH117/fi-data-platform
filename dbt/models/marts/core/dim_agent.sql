-- Selling agents (Admin Core is the master; legacy producer codes resolve to the same agent_code).
select
    md5('agent|' || cast(agent_id as varchar)) as agent_key,
    agent_id,
    agent_code,
    agent_name,
    territory,
    tenant_id as tenant_key,
    coalesce(is_advance_agreement, 0) = 1 as has_advance_agreement,
    {{ audit_columns() }}
from {{ ref('stg_admin_core__agent') }}
where not is_deleted

union all

select '-1', null, 'UNKNOWN', 'Unknown agent', null, null, false, current_timestamp
