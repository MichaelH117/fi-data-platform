-- V7 edit 46: the dealer incentive statement, summed over every remittance, equals the
-- PRODUCER_INCENTIVE bucket of the cost-split ledger per dealer (nothing dropped, nothing counted twice).
with stmt as (
    select dealer_key, sum(total_due) as statement_total
    from {{ ref('mart_dealer_incentive_statement') }}
    group by 1
),

ledger as (
    select dealer_key, sum(amount) as ledger_total
    from {{ ref('fct_disbursement') }}
    where incentive_group = 'PRODUCER_INCENTIVE'
    group by 1
)

select *
from stmt
full outer join ledger using (dealer_key)
where abs(coalesce(statement_total, 0) - coalesce(ledger_total, 0)) > 0.5
