-- Diagnostic: share of ALL claims (both systems) that resolve to a contract_key, by source system
-- and overall. Claims entered without a contract key are kept and reported, but coverage below
-- var min_claim_contract_coverage (98%) fails the gate.
with claims as (
    select source_system, has_contract_key from {{ ref('int_claim_number_bridge') }}
),

coverage as (
    select source_system, count(*) as claims, count(*) filter (where has_contract_key) as with_contract
    from claims
    group by 1
    union all
    select 'ALL', count(*), count(*) filter (where has_contract_key)
    from claims
)

select *, with_contract / claims as coverage, {{ var('min_claim_contract_coverage') }} as threshold
from coverage
where with_contract / claims < {{ var('min_claim_contract_coverage') }}
