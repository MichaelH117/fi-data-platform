-- Call-center contact -> conformed contract_key. The CRM knows a customer by contract_number (when
-- the agent typed it) and/or phone_hash. Rule, in order:
--   CONTRACT_NUMBER  the contract number resolves through int_contract_key_bridge
--   PHONE_HASH       no usable number, and the phone hash matches exactly ONE Admin Core contract
--                    of the same tenant (a phone shared by several contracts is never guessed)
--   UNRESOLVED       neither (contract_key = '-1'; kept, counted, never dropped)
with calls as (
    select c.*, t.tenant_key
    from {{ ref('stg_crm__calls') }} as c
    left join {{ ref('dim_tenant') }} as t on t.tenant_code = c.tenant_code
),

by_number as (
    select contract_number, contract_key
    from {{ ref('int_contract_key_bridge') }}
    where not is_deleted
),

by_phone as (
    select k.tenant_id as tenant_key, k.phone_hash, any_value(b.contract_key) as contract_key
    from {{ ref('stg_admin_core__contract') }} as k
    inner join {{ ref('int_contract_key_bridge') }} as b
        on b.source_system = 'admin_core' and b.admin_contract_id = k.contract_id
    where not k.is_deleted and k.phone_hash is not null
    group by 1, 2
    having count(*) = 1
)

select
    c.call_id,
    c.tenant_key,
    coalesce(n.contract_key, p.contract_key, '-1') as contract_key,
    case
        when n.contract_key is not null then 'CONTRACT_NUMBER'
        when p.contract_key is not null then 'PHONE_HASH'
        else 'UNRESOLVED'
    end as contract_match_method
from calls as c
left join by_number as n on n.contract_number = c.contract_number
left join by_phone as p on n.contract_key is null and p.tenant_key = c.tenant_key and p.phone_hash = c.phone_hash
