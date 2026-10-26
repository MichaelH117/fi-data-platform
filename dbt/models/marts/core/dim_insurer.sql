-- Insurers. V6 edit 3: program_type (FULL_INSURED | AGENCY | PASS_THROUGH) comes from the source row,
-- never derived in a mart, so filings, disbursement routing and pages share one value.
select
    md5('insurer|' || cast(insurer_id as varchar)) as insurer_key,
    insurer_id,
    'INSURER' as entity_type,
    insurer_name,
    program_type,
    {{ audit_columns() }}
from {{ ref('stg_admin_core__insurer') }}
where not is_deleted

union all

select '-1', null, 'INSURER', 'Unknown insurer', 'UNKNOWN', current_timestamp
