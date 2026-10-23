-- Close control: every posted transaction's cost split adds back to the transaction (within the
-- rounding tolerance per split line, seed control_tolerances) and every transaction has a split.
-- Also: no split line is orphaned (its total must equal fct_disbursement's grand total).
select 'unbalanced' as failure, posting_period_key, source_system, txn_type, difference as amount
from {{ ref('mart_disbursement_control_totals') }}
where not is_balanced

union all

select 'orphan_split', null, null, null, m.split_amount - d.amount
from (select sum(split_amount) as split_amount from {{ ref('mart_disbursement_control_totals') }}) as m,
    (select sum(amount) as amount from {{ ref('fct_disbursement') }}) as d
where abs(m.split_amount - d.amount) > 0.01
