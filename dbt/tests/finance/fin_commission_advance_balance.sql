-- An agent's advance balance is the advance it has not yet earned. It must never be negative and
-- never exceed the unearned commission on its in-force advance contracts (advance x (1 - cumulative
-- earned share), contracts with no chargeback), recomputed here from the core facts at each agent's
-- latest statement period. Tolerance: one cent per period of rounding.
with adv as (
    select agent_key, tenant_key, contract_key,
        sum(amount) filter (where amount > 0) as advance_amount,
        count(*) filter (where amount < 0) as chargebacks
    from {{ ref('fct_commission') }}
    where is_advance
    group by 1, 2, 3
),

earned_share as (
    select contract_key, max(earned_pct_cumulative) as earned_share
    from {{ ref('fct_earned_disbursement') }}
    where disbursement_type = 'COMMISSION'
    group by 1
),

unearned as (
    select a.agent_key, a.tenant_key,
        sum(a.advance_amount * (1 - coalesce(e.earned_share, 0))) as unearned_in_force
    from adv as a
    left join earned_share as e using (contract_key)
    where a.chargebacks = 0
    group by 1, 2
),

stmt as (
    select agent_key, tenant_key,
        arg_max(advance_balance, posting_period_key) as advance_balance,
        count(*) as periods
    from {{ ref('mart_commission_statement') }}
    group by 1, 2
)

select s.agent_key, s.tenant_key, s.advance_balance, coalesce(u.unearned_in_force, 0) as unearned_in_force
from stmt as s
left join unearned as u using (agent_key, tenant_key)
where s.advance_balance < -0.01 * s.periods
    or s.advance_balance > coalesce(u.unearned_in_force, 0) + 0.01 * s.periods
