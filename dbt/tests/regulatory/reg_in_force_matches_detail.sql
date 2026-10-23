-- Reconciliation: the engine's in-force count (summary, ITD) equals the contract detail tab built
-- from the window flags, per state and provider. Two code paths, one number.
with summary as (
    select state, provider_key, contracts_in_force
    from {{ ref('mart_funded_reserve_summary') }}
    where scope = 'ITD' and basis = 'COVERAGE_COST'
),

detail as (
    select state, provider_key, count(*) as contracts_in_force
    from {{ ref('mart_funded_reserve_contracts') }}
    where has_requirement and is_filing_product_scope and is_in_force_at_period_end
    group by all
)

select coalesce(s.state, d.state) as state, coalesce(s.provider_key, d.provider_key) as provider_key,
       s.contracts_in_force as summary_count, d.contracts_in_force as detail_count
from summary as s
full outer join detail as d using (state, provider_key)
where coalesce(s.contracts_in_force, 0) <> coalesce(d.contracts_in_force, 0)
