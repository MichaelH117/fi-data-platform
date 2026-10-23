-- Bridge from the commission statement to the P&L: by tenant x posting period, the statement's
-- commission earned on the curve (commission expensed - expensed ahead of the curve) must equal the
-- P&L earned_commission (mart_pnl_by_product, the acquisition line). Both start from
-- fct_earned_disbursement COMMISSION; this proves no contract is dropped when the earning is split
-- over agents and the legacy reconciling row.
with stmt as (
    select tenant_key, posting_period_key,
        sum(commission_expensed - expensed_ahead_of_curve) as amount
    from {{ ref('mart_commission_statement') }}
    group by 1, 2
),

pnl as (
    select tenant_key, posting_period_key, sum(earned_commission) as amount
    from {{ ref('mart_pnl_by_product') }}
    group by 1, 2
)

select tenant_key, posting_period_key, s.amount as statement_on_curve, p.amount as pnl_earned_commission
from stmt as s
full outer join pnl as p using (tenant_key, posting_period_key)
where abs(coalesce(s.amount, 0) - coalesce(p.amount, 0)) > 0.01
