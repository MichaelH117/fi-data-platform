-- Diagnostic: every contract reference (claims, cancellations, amendments, transactions, cash)
-- resolves through int_contract_key_bridge for >= var min_contract_bridge_coverage (99%) of rows.
-- Returns one row per failing reference type.
with bridge as (
    select contract_key from {{ ref('int_contract_key_bridge') }} where not is_deleted
),

refs as (
    select 'claim' as reference_type, referenced_contract_key as contract_key
    from {{ ref('int_claim_number_bridge') }}
    where referenced_contract_key is not null

    union all
    select 'cancellation', contract_key from {{ ref('int_cancellations_conformed') }}

    union all
    select 'transaction', contract_key from {{ ref('int_transactions_posted') }}

    union all
    select 'cash', contract_key from {{ ref('int_remittance_lifecycle') }}

    union all
    select
        'amendment',
        case
            when contract_id is not null then {{ contract_key("'admin_core'", 'contract_id') }}
            else {{ contract_key("'legacy_admin'", 'legacy_contract_ref') }}
        end
    from {{ ref('stg_admin_core__contract_amendment') }}
    where not is_deleted and (contract_id is not null or legacy_contract_ref is not null)
),

coverage as (
    select
        r.reference_type,
        count(*) as references_total,
        count(b.contract_key) as resolved,
        count(b.contract_key) / count(*) as coverage
    from refs as r
    left join bridge as b using (contract_key)
    group by 1
)

select *, {{ var('min_contract_bridge_coverage') }} as threshold
from coverage
where coverage < {{ var('min_contract_bridge_coverage') }}
