-- Producer, with its ROLE made explicit (docs/V3_DESIGN.md 8.4). "Producer" means two different
-- things in VSC admin data, and filtering the wrong one zeroes a book:
--   selling_dealer    the dealer (or agency) that sold the contract     producer_key = dealer_key
--   provider_entity   the obligor / provider entity that backs it       producer_key = obligor_key
-- Contracts carry both, by name: dim_contract.selling_dealer_key and dim_contract.provider_entity_key.
-- Keys never collide: dealer and obligor keys are md5 over different namespaces. '-1' = unknown.
select
    d.dealer_key as producer_key,
    'selling_dealer' as producer_role,
    d.dealer_number as producer_code,
    d.dealer_name as producer_name,
    d.state,
    d.tenant_key,
    {{ audit_columns() }}
from {{ ref('dim_dealer') }} as d
where d.dealer_key <> '-1'

union all

select
    o.obligor_key,
    'provider_entity',
    coalesce('OBL' || lpad(cast(o.obligor_id as varchar), 2, '0'), 'DOO' || cast(o.participation_entity_id as varchar)),
    o.obligor_name,
    o.state,
    null,
    current_timestamp
from {{ ref('dim_obligor') }} as o
where o.obligor_key <> '-1'

union all

select '-1', 'unknown', 'UNKNOWN', 'Unknown producer', null, null, current_timestamp
