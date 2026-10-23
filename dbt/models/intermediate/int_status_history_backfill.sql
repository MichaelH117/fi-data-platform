{{ config(materialized='table') }}
-- Contract and claim status history BEFORE (and alongside) the daily snapshots, so history never
-- starts late. Admin Core keeps contract_status_history and claim_history; legacy history is
-- rebuilt from its dates (sale -> INFORCE, cancel -> CANCELLED, expiry -> EXPIRED; claim
-- reported -> OPEN, then paid/denied). One row per entity status interval.
with contract_events as (
    select
        {{ contract_key("'admin_core'", 'contract_id') }} as entity_key,
        changed_at,
        to_status as status,
        'admin_core' as source_system
    from {{ ref('stg_admin_core__contract_status_history') }}
    where not is_deleted

    union all
    select contract_key, cast(entered_date as timestamp), 'INFORCE', 'legacy_admin'
    from {{ ref('int_contracts_conformed') }}
    where source_system = 'legacy_admin'

    union all
    select x.contract_key, cast(x.cancel_date as timestamp), 'CANCELLED', 'legacy_admin'
    from {{ ref('int_cancellations_conformed') }} as x
    where x.source_system = 'legacy_admin' and x.is_effective_cancel

    union all
    select c.contract_key, cast(c.expiration_date as timestamp), 'EXPIRED', 'legacy_admin'
    from {{ ref('int_contracts_conformed') }} as c
    cross join {{ ref('int_as_of_date') }} as a
    where c.source_system = 'legacy_admin' and c.status = 'EXPIRED' and c.expiration_date <= a.as_of_date
),

claim_events as (
    select
        b.claim_key as entity_key,
        h.changed_at,
        h.to_status as status,
        'admin_core' as source_system
    from {{ ref('stg_admin_core__claim_history') }} as h
    inner join {{ ref('int_claim_number_bridge') }} as b
        on b.admin_claim_id = h.claim_id and b.source_system = 'admin_core'
    where not h.is_deleted

    union all
    select claim_key, cast(reported_date as timestamp), 'OPEN', 'legacy_admin'
    from {{ ref('int_claims_conformed') }}
    where source_system = 'legacy_admin'

    union all
    select c.claim_key, cast(coalesce(l.paid_date, l.updated_at) as timestamp), c.status, 'legacy_admin'
    from {{ ref('int_claims_conformed') }} as c
    inner join {{ ref('stg_legacy_admin__claims') }} as l on l.claim_number = c.claim_number
    where c.source_system = 'legacy_admin' and c.status <> 'OPEN'
),

events as (
    select 'contract' as entity_type, * from contract_events
    union all
    select 'claim', * from claim_events
)

select
    entity_type,
    entity_key,
    source_system,
    status,
    changed_at as valid_from,
    lead(changed_at) over w as valid_to,
    lead(changed_at) over w is null as is_current
from events
where changed_at is not null and status is not null
window w as (partition by entity_type, entity_key order by changed_at, status)
