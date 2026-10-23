{{ config(materialized='table') }}
-- One row per claim across both systems, on claim_key = md5('claim|' || ltrim(claim_no, '0')).
-- '0000123' (legacy) and '123' (Admin Core) are the same claim: claims open at the cutover were
-- converted into Admin Core, which is then the system of record (is_converted = true) and the
-- legacy copy is dropped. Admin Core claims on legacy contracts carry legacy_contract_ref.
-- A converted claim whose Admin Core copy lost its contract link (contract_id and
-- legacy_contract_ref both null) falls back to the legacy copy's contract, so it never becomes an
-- orphan. Tests: every converted claim resolves to a contract, and agrees with the legacy copy on
-- contract and loss date (claim numbers alone match across overlapping keyspaces).
with core as (
    select
        claim_id,
        {{ normalize_claim_number('claim_number') }} as claim_number,
        tenant_id,
        case
            when contract_id is not null then {{ contract_key("'admin_core'", 'contract_id') }}
            when legacy_contract_ref is not null then {{ contract_key("'legacy_admin'", 'legacy_contract_ref') }}
        end as referenced_contract_key
    from {{ ref('stg_admin_core__claim') }}
    where not is_deleted
),

legacy as (
    select
        claim_number,
        {{ contract_key("'legacy_admin'", 'legacy_contract_id') }} as referenced_contract_key
    from {{ ref('stg_legacy_admin__claims') }}
    where not is_deleted
),

core_resolved as (
    select
        core.claim_id,
        core.claim_number,
        core.tenant_id,
        coalesce(core.referenced_contract_key, legacy.referenced_contract_key) as referenced_contract_key,
        legacy.claim_number is not null as is_converted,
        core.referenced_contract_key is null and legacy.referenced_contract_key is not null
            as is_contract_from_legacy_copy
    from core
    left join legacy using (claim_number)
),

unioned as (
    select
        claim_number,
        'admin_core' as source_system,
        cast(claim_id as varchar) as source_claim_id,
        claim_id as admin_claim_id,
        referenced_contract_key,
        is_converted,
        is_contract_from_legacy_copy
    from core_resolved

    union all

    select
        claim_number,
        'legacy_admin',
        claim_number,
        null,
        referenced_contract_key,
        false,
        false
    from legacy
    where claim_number not in (select claim_number from core where claim_number is not null)
)

select
    {{ claim_key('u.claim_number') }} as claim_key,
    u.claim_number,
    u.source_system,
    u.source_claim_id,
    u.admin_claim_id,
    u.is_converted,
    u.is_contract_from_legacy_copy,
    u.referenced_contract_key,
    b.contract_key,
    b.contract_key is not null as has_contract_key
from unioned as u
left join {{ ref('int_contract_key_bridge') }} as b
    on b.contract_key = u.referenced_contract_key and not b.is_deleted
