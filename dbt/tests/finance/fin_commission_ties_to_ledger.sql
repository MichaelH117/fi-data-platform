-- The commission statement must tie to the commission sub-ledger (fct_commission) per agent; no
-- commission line may fall out of the statement. The NO_AGENT_LEDGER reconciling row (cost-split
-- commission on legacy contracts) is not in the ledger; fin_commission_ties_to_cost_split covers it.
with stmt as (
    select agent_key, tenant_key, sum(net_commission) as amount, sum(commission_lines) as lines
    from {{ ref('mart_commission_statement') }}
    where not is_no_agent_ledger
    group by 1, 2
),

ledger as (
    select agent_key, tenant_key, sum(amount) as amount, count(*) as lines
    from {{ ref('fct_commission') }}
    group by 1, 2
)

select agent_key, tenant_key, s.amount, l.amount as ledger_amount, s.lines, l.lines as ledger_lines
from stmt as s
full outer join ledger as l using (agent_key, tenant_key)
where abs(coalesce(s.amount, 0) - coalesce(l.amount, 0)) > 0.01
    or coalesce(s.lines, 0) <> coalesce(l.lines, 0)
