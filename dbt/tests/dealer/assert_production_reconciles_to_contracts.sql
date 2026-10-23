-- Reconcile before publishing: dealer production totals (count and coverage cost) must equal the
-- core contract dims filtered by the same seed status set.
with expected as (
    select count(*) as contracts, round(sum(coalesce(f.coverage_cost, 0)), 2) as coverage_cost
    from {{ ref('dim_contract') }} as c
    inner join {{ ref('dealer_report_status_sets') }} as s
        on s.report_id = 'DEALER_PRODUCTION' and s.status = c.status
    left join {{ ref('dim_contract_finance') }} as f using (contract_key)
    where c.contract_key <> '-1'
),

actual as (
    select sum(contracts_written) as contracts, round(sum(coverage_cost), 2) as coverage_cost
    from {{ ref('mart_dealer_production') }}
)

select e.contracts as expected_contracts, a.contracts as actual_contracts,
    e.coverage_cost as expected_coverage_cost, a.coverage_cost as actual_coverage_cost
from expected as e cross join actual as a
where e.contracts <> a.contracts or abs(e.coverage_cost - a.coverage_cost) > 1.0
