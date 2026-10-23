select
    md5('dealer_group|' || cast(dealer_group_id as varchar)) as dealer_group_key,
    dealer_group_id,
    group_code,
    group_name,
    tenant_id as tenant_key,
    {{ audit_columns() }}
from {{ ref('stg_admin_core__dealer_group') }}
where not is_deleted

union all

select '-1', null, 'NONE', 'No dealer group', null, current_timestamp
