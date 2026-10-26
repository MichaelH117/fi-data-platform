-- Participation entity x program type (V6 edit 13). One row per entity and each program type its
-- dealer writes, so run-off and transfer reports split a ceded book by FULL_INSURED / AGENCY /
-- PASS_THROUGH. contracts_ceded counts contracts entered on or before the entity's run-off date
-- (all of them while ACTIVE); contracts_retained are entered after it, and their reserve stays with
-- the obligor (the same rule the source applies when it routes the money).
with contracts as (
    select k.dealer_key, k.entered_date, f.program_type
    from {{ ref('dim_contract') }} as k
    inner join {{ ref('dim_contract_finance') }} as f using (contract_key)
    where k.contract_key <> '-1'
)

select
    md5('participation_program|' || e.participation_entity_key || '|' || c.program_type)
        as participation_entity_program_key,
    e.participation_entity_key,
    c.program_type,
    e.status,
    e.runoff_date,
    e.cession_type,
    count(*) as contracts,
    count(*) filter (where e.runoff_date is null or c.entered_date <= e.runoff_date) as contracts_ceded,
    count(*) filter (where e.runoff_date is not null and c.entered_date > e.runoff_date) as contracts_retained,
    min(c.entered_date) as first_entered_date,
    max(c.entered_date) as last_entered_date,
    {{ audit_columns() }}
from {{ ref('dim_participation_entity') }} as e
inner join contracts as c using (dealer_key)
group by e.participation_entity_key, c.program_type, e.status, e.runoff_date, e.cession_type
