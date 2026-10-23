-- The dbt snapshots are a cross-check on the replayable version history (int_*_versions, built
-- from raw): the current snapshot row of every live entity must carry the status of its newest
-- raw version. A mismatch means a snapshot missed or invented a change.
with snap_contract as (
    select contract_key as entity_key, status from {{ ref('snap_contract_status') }} where dbt_valid_to is null
),
snap_claim as (
    select claim_key as entity_key, status from {{ ref('snap_claim_status') }} where dbt_valid_to is null
),
-- a claim converted into Admin Core is current on its Admin Core copy
cur_contract as (
    select contract_key as entity_key, status from {{ ref('int_contract_versions') }}
    where is_current and not is_deleted
),
cur_claim as (
    select claim_key as entity_key, status from {{ ref('int_claim_versions') }}
    where not is_deleted
    qualify row_number() over (
        partition by claim_key order by source_system = 'admin_core' desc, version_no desc
    ) = 1
)

select 'contract' as entity, s.entity_key, s.status as snapshot_status, v.status as version_status
from snap_contract as s
inner join cur_contract as v using (entity_key)
where s.status is distinct from v.status

union all

select 'claim', s.entity_key, s.status, v.status
from snap_claim as s
inner join cur_claim as v using (entity_key)
where s.status is distinct from v.status
