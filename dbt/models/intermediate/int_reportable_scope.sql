{{ config(materialized='table') }}
-- THE one place demo dealers, the test contract ranges and API test customers are excluded
-- (seeds/core/demo_exclusions.csv).
-- One row per conformed contract. Every core fact and mart keeps only is_reportable rows by
-- joining here; diagnostics check that no non-reportable row reaches a mart.
with rules as (
    select * from {{ ref('demo_exclusions') }}
),

contracts as (
    select contract_key, source_system, source_contract_id, contract_number, dealer_number
    from {{ ref('int_contracts_conformed') }}
),

-- API test contracts carry a placeholder customer name. Names live only in staging, so the match
-- runs here against staging and only the contract key + rule leave this model.
customer_names as (
    select {{ contract_key("'admin_core'", 'contract_id') }} as contract_key, 'admin_core' as source_system,
           upper(trim(concat_ws(' ', customer_first_name, customer_last_name))) as full_name
    from {{ ref('stg_admin_core__contract') }}
    union all
    select {{ contract_key("'legacy_admin'", 'legacy_contract_id') }}, 'legacy_admin',
           upper(trim(concat_ws(' ', customer_first_name, customer_last_name)))
    from {{ ref('stg_legacy_admin__contracts') }}
),

test_customers as (
    select n.contract_key, min(r.rule_id) as rule_id
    from customer_names as n
    inner join rules as r
        on r.rule_type = 'test_customer_name'
        and r.source_system in (n.source_system, 'any')
        and regexp_full_match(n.full_name, r.contract_number_pattern)
    group by 1
),

flagged as (
    select
        c.*,
        dlr.exclusion_rule_id as dealer_rule_id,
        rng.rule_id as id_range_rule_id,
        coalesce(pat.rule_id, tc.rule_id) as number_rule_id
    from contracts as c
    left join test_customers as tc using (contract_key)
    left join {{ ref('int_reportable_dealers') }} as dlr
        on dlr.dealer_number = c.dealer_number and not dlr.is_reportable
    left join rules as rng
        on rng.rule_type = 'contract_id_range'
        and rng.source_system in (c.source_system, 'any')
        and try_cast(c.source_contract_id as bigint) between rng.id_from and rng.id_to
    left join rules as pat
        on pat.rule_type = 'contract_number_pattern'
        and pat.source_system in (c.source_system, 'any')
        and regexp_full_match(c.contract_number, pat.contract_number_pattern)
    qualify row_number() over (partition by c.contract_key order by dealer_rule_id, id_range_rule_id, number_rule_id) = 1
)

select
    contract_key,
    source_system,
    contract_number,
    dealer_number,
    dealer_rule_id is not null as is_demo_dealer,
    coalesce(id_range_rule_id, number_rule_id) is not null as is_test_contract,
    coalesce(dealer_rule_id, id_range_rule_id, number_rule_id) as exclusion_rule_id,
    coalesce(dealer_rule_id, id_range_rule_id, number_rule_id) is null as is_reportable
from flagged
