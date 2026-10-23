-- Row-count reconciliation raw -> core: every live source contract and claim is either in core or
-- excluded by a documented rule (demo dealer / test range in int_reportable_scope). 0 rows tolerance.
-- Claims are also tied per status group (status read from staging, not from the conformed model),
-- so a join or filter that loses open, pending or denied claims fails as its own row.
with live_contracts as (
    select count(*) as n from {{ ref('int_contract_key_bridge') }} where not is_deleted
),

claims as (
    select count(*) as n from {{ ref('int_claim_number_bridge') }}
),

bridge_status as (
    select
        b.claim_key,
        case
            when coalesce(c.status, upper(l.claim_status)) = 'DENIED' then 'denied'
            when coalesce(c.status, '') like 'CLOSED%' or l.claim_status = 'paid' then 'paid'
            else 'open'
        end as status_group
    from {{ ref('int_claim_number_bridge') }} as b
    left join {{ ref('stg_admin_core__claim') }} as c
        on b.source_system = 'admin_core' and c.claim_id = b.admin_claim_id
    left join {{ ref('stg_legacy_admin__claims') }} as l
        on b.source_system = 'legacy_admin' and l.claim_number = b.source_claim_id and not l.is_deleted
),

excluded_claims as (
    select c.claim_key
    from {{ ref('int_claims_conformed') }} as c
    inner join {{ ref('int_reportable_scope') }} as s using (contract_key)
    where not s.is_reportable
),

by_status as (
    select
        bs.status_group,
        count(*) as raw_rows,
        count(*) filter (where bs.claim_key in (select claim_key from excluded_claims)) as excluded_rows
    from bridge_status as bs
    group by bs.status_group
),

core_by_status as (
    select status_group, count(*) as core_rows from {{ ref('dim_claim') }} group by status_group
)

select 'contracts' as entity,
    (select n from live_contracts) as raw_rows,
    (select count(*) from {{ ref('dim_contract') }} where contract_key <> '-1') as core_rows,
    (select count(*) from {{ ref('int_reportable_scope') }} where not is_reportable) as excluded_rows
where (select n from live_contracts)
    <> (select count(*) from {{ ref('dim_contract') }} where contract_key <> '-1')
     + (select count(*) from {{ ref('int_reportable_scope') }} where not is_reportable)

union all

select 'claims',
    (select n from claims),
    (select count(*) from {{ ref('dim_claim') }}),
    (select count(*) from excluded_claims)
where (select n from claims)
    <> (select count(*) from {{ ref('dim_claim') }}) + (select count(*) from excluded_claims)

union all

select 'claims (' || coalesce(r.status_group, k.status_group) || ')',
    coalesce(r.raw_rows, 0),
    coalesce(k.core_rows, 0),
    coalesce(r.excluded_rows, 0)
from by_status as r
full outer join core_by_status as k using (status_group)
where coalesce(r.raw_rows, 0) <> coalesce(k.core_rows, 0) + coalesce(r.excluded_rows, 0)
