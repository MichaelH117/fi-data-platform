-- Attribution check: for structures that own the claims reserve, the reserve the ledger pays to the
-- entity's DEALER_RESERVE account holder must equal the reserve on that entity's dealer's Admin
-- Core contracts (the dealer -> entity rule the cession mart uses). Legacy has no account holders.
with entity as (
    select distinct participation_entity_key, entity_id, dealer_key
    from {{ ref('mart_cession_experience') }}
    where owns_claims_reserve
),

to_holder as (
    select en.participation_entity_key, sum(d.amount) as amount
    from {{ ref('fct_disbursement') }} as d
    inner join {{ ref('dim_account_holder') }} as h using (account_holder_key)
    inner join entity as en on h.holder_type = 'DEALER_RESERVE' and h.ref_id = en.entity_id
    where d.disbursement_type = 'CLAIMS_RESERVE'
    group by 1
),

by_dealer as (
    -- ceded contracts only: after a run-off date the ledger pays the obligor, not the entity
    select en.participation_entity_key, sum(d.amount) as amount
    from {{ ref('fct_disbursement') }} as d
    inner join {{ ref('mart_participation_contract') }} as m using (contract_key)
    inner join entity as en using (participation_entity_key)
    where d.disbursement_type = 'CLAIMS_RESERVE' and d.source_system = 'admin_core' and m.is_ceded
    group by 1
)

select participation_entity_key, h.amount as ledger_amount, b.amount as dealer_amount
from to_holder as h
full outer join by_dealer as b using (participation_entity_key)
where abs(coalesce(h.amount, 0) - coalesce(b.amount, 0)) > 0.01
