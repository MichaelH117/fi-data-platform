-- Legacy dealers: latest version per cleaned dealer code ('d1039 ' and 'D1039' are one dealer).
with cleaned as (
    select *, {{ clean_code('dealer_code') }} as dealer_code_clean
    from {{ source('legacy_admin', 'dealers') }}
),

latest as (
    {{ latest_version('cleaned', 'dealer_code_clean') }}
)

select
    dealer_code_clean as dealer_code,
    trim(dealer_name) as dealer_name,
    trim(city) as city,
    upper(trim(state)) as state,
    {{ clean_code('agent_id') }} as agent_id,
    trim(reinsurance_company) as reinsurance_company,
    lower(trim(status)) as dealer_status,
    onboarded_date,
    created_at,
    updated_at,
    is_deleted,
    _source_system,
    _loaded_at
from latest
where dealer_code_clean is not null
