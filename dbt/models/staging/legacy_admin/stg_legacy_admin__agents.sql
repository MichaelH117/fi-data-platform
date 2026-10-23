-- Legacy agents: latest version per agent code. Email is PII and is dropped.
with cleaned as (
    select *, {{ clean_code('agent_id') }} as agent_code
    from {{ source('legacy_admin', 'agents') }}
),

latest as (
    {{ latest_version('cleaned', 'agent_code', exclude=['email', 'agent_id']) }}
)

select * from latest
where agent_code is not null
