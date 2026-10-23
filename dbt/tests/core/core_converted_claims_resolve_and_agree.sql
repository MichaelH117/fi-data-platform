-- A converted claim (claim number in both systems; Admin Core is the record) must resolve to a
-- contract, and when both copies name a contract they must name the same one, so a claim-number
-- collision across the two overlapping keyspaces cannot pass as a conversion.
with core as (
    select
        {{ normalize_claim_number('claim_number') }} as claim_number,
        case
            when contract_id is not null then {{ contract_key("'admin_core'", 'contract_id') }}
            when legacy_contract_ref is not null then {{ contract_key("'legacy_admin'", 'legacy_contract_ref') }}
        end as core_contract_key
    from {{ ref('stg_admin_core__claim') }}
    where not is_deleted
),

legacy as (
    select
        claim_number,
        {{ contract_key("'legacy_admin'", 'legacy_contract_id') }} as legacy_contract_key
    from {{ ref('stg_legacy_admin__claims') }}
    where not is_deleted
)

select b.claim_number, b.has_contract_key, c.core_contract_key, l.legacy_contract_key
from {{ ref('int_claim_number_bridge') }} as b
inner join core as c using (claim_number)
inner join legacy as l using (claim_number)
where b.is_converted
  and (not b.has_contract_key
       or (c.core_contract_key is not null and l.legacy_contract_key is not null
           and c.core_contract_key <> l.legacy_contract_key))
