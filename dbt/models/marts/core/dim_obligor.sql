-- Obligor entities. V6 edit 6: obligor_kind splits who backs the contract:
--   internal_provider  the provider's own obligor companies (one per state; contracts carry an
--                      original and a current obligor id from this list)
--   dealer_owned       a dealer-owned obligor participation entity (structure DEALER_OWNED_OBLIGOR);
--                      keyed in its own namespace, so the keys never collide with the internal ones
select
    md5('obligor|' || cast(obligor_id as varchar)) as obligor_key,
    obligor_id,
    'OBLIGOR' as entity_type,
    'internal_provider' as obligor_kind,
    obligor_name,
    state,
    cast(null as bigint) as participation_entity_id,
    {{ audit_columns() }}
from {{ ref('stg_admin_core__obligor') }}
where not is_deleted

union all

select
    md5('obligor|dealer_owned|' || cast(e.entity_id as varchar)),
    null,
    'OBLIGOR',
    'dealer_owned',
    e.entity_name,
    d.state,
    e.entity_id,
    {{ audit_columns() }}
from {{ ref('stg_admin_core__participation_entity') }} as e
left join {{ ref('stg_admin_core__dealer') }} as d using (dealer_id)
where not e.is_deleted and e.structure_type = 'DEALER_OWNED_OBLIGOR'

union all

select '-1', null, 'OBLIGOR', 'internal_provider', 'Unknown obligor', null, null, current_timestamp
