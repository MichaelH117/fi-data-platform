-- Cost-split commission = agent ledger + legacy unassigned. By source system x tenant x posting
-- period, the statement's net commission (agent rows from fct_commission plus the NO_AGENT_LEDGER
-- reconciling row) must equal the COMMISSION bucket of the cost split (fct_disbursement), so no
-- commission dollar in the split is left without a payee or a reconciling line. Tolerance: half a
-- cent per line (seed control_tolerances split_rounding_per_line) plus one cent.
with split as (
    select source_system, tenant_key, posting_period_key, sum(amount) as amount, count(*) as lines
    from {{ ref('fct_disbursement') }}
    where disbursement_type = 'COMMISSION'
    group by 1, 2, 3
),

stmt as (
    select source_system, tenant_key, posting_period_key, sum(net_commission) as amount,
        sum(commission_lines) as lines
    from {{ ref('mart_commission_statement') }}
    group by 1, 2, 3
)

select source_system, tenant_key, posting_period_key, s.amount as split_amount, t.amount as statement_amount
from split as s
full outer join stmt as t using (source_system, tenant_key, posting_period_key)
where abs(coalesce(s.amount, 0) - coalesce(t.amount, 0))
    > 0.005 * greatest(coalesce(s.lines, 0), coalesce(t.lines, 0)) + 0.01
