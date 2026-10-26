-- Diagnostic (V7 edit 28): commission with no manager is shown, never dropped. A contract whose
-- agent has no district lands in the explicit UNASSIGNED row of mart_commission_hierarchy. The
-- measured value (pipeline diagnostics) is how many contracts sit in that row; that count never
-- fails. The check fails when a posting period's national commission differs from the commission
-- ledger (fct_commission) by more than a cent: money that fell out of the hierarchy silently.
with ledger as (
    select posting_period_key, sum(amount) as ledger_commission
    from {{ ref('fct_commission') }}
    group by 1
),

hierarchy as (
    select posting_period_key, sum(commission) as hierarchy_commission
    from {{ ref('mart_commission_hierarchy') }}
    where level = 'NATIONAL'
    group by 1
)

select
    posting_period_key,
    coalesce(l.ledger_commission, 0) as ledger_commission,
    coalesce(h.hierarchy_commission, 0) as hierarchy_commission
from ledger as l
full outer join hierarchy as h using (posting_period_key)
where abs(coalesce(l.ledger_commission, 0) - coalesce(h.hierarchy_commission, 0)) > 0.01
