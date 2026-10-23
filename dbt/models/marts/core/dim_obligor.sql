-- Obligor entities (one per state). Contracts carry an original and a current obligor.
select
    md5('obligor|' || cast(obligor_id as varchar)) as obligor_key,
    obligor_id,
    'OBLIGOR' as entity_type,
    obligor_name,
    state,
    {{ audit_columns() }}
from {{ ref('stg_admin_core__obligor') }}
where not is_deleted

union all

select '-1', null, 'OBLIGOR', 'Unknown obligor', null, current_timestamp
