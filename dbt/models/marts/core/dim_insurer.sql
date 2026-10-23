select
    md5('insurer|' || cast(insurer_id as varchar)) as insurer_key,
    insurer_id,
    'INSURER' as entity_type,
    insurer_name,
    {{ audit_columns() }}
from {{ ref('stg_admin_core__insurer') }}
where not is_deleted

union all

select '-1', null, 'INSURER', 'Unknown insurer', current_timestamp
