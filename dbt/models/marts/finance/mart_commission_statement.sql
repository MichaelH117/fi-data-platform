-- Commission statement. Grain: agent x tenant x posting period (periods with commission activity,
-- an advance earning down, or commission earning on the cost-split curve), plus one reconciling row
-- per tenant x posting period x source system for cost-split commission with no agent-ledger line.
-- Agent rows (source_system 'admin_core': fct_commission is the Admin Core agent ledger): commission
-- paid on new business (standard plan), advances paid (advance plan, is_advance), and chargebacks on
-- cancels (negative, both plans).
-- Advance plan: the advance is earned down on the contract's own commission earning curve
-- (fct_earned_disbursement COMMISSION earned_pct) and that earning is booked as a recovery of the
-- advance. When the contract cancels, the rest of its advance is released (expensed) in the
-- chargeback period and the chargeback is booked like any other, so a cancelled contract carries no
-- advance balance.
-- Reconciling row (agent_key 'NO_AGENT_LEDGER', is_no_agent_ledger): the COMMISSION bucket of the
-- cost split (fct_disbursement) on contracts that have no agent-ledger line, which today is every
-- legacy contract (the legacy system never fed an agent ledger). So, by source system and period,
-- statement net_commission = cost-split COMMISSION bucket (test fin_commission_ties_to_cost_split).
--   commission_written     = commission_paid + advances_paid (gross commission put on the books)
--   commission_expensed    = standard commission paid + advance earned down (cash/expense basis: a
--                            standard-plan commission is expensed when it is paid at sale)
--   commission_chargebacks = every negative line (always <= 0)
--   advances_net           = advances_paid - advance_recoveries (cash advanced not yet expensed)
--   net_commission         = commission_expensed + commission_chargebacks + advances_net (cash)
--   chargeback_rate        = -chargebacks / commission_written (the same base for both plans)
--   commission_earned_on_curve = fct_earned_disbursement COMMISSION earned_amount (the P&L
--                            earned_commission basis), split over a contract's agents by their share
--                            of its positive ledger lines; ties to mart_pnl_by_product
--                            (test fin_commission_bridges_to_pnl)
--   expensed_ahead_of_curve = commission_expensed - commission_earned_on_curve: the bridge from the
--                            statement to the P&L acquisition line (commission paid at sale, or an
--                            advance released on cancel, before the contract's curve earns it)
--   advance_balance        = ITD advances paid - ITD advance recoveries (unearned advance still
--                            held by the agent = unearned commission on in-force advance contracts)
--   contracts_commissioned = distinct contracts with a positive line in this period. NOT additive over
--                            periods (an amended, reinstated or re-paid contract shows up again).
--   contracts_first_commissioned = contracts whose first positive line for this agent is in this
--                            period: additive over periods for one agent (each contract once per agent;
--                            a contract split between agents counts once for each of them).
--   contracts_first_commissioned_primary = the same, counted only for the contract's primary agent
--                            (largest share of its positive lines, then lowest agent_key): additive over
--                            agents and periods, so a sum is distinct contracts and ties to sales
--                            (test fin_commission_contracts_within_sales).
with lines as (
    select
        agent_key,
        tenant_key,
        posting_period_key,
        count(distinct contract_key) filter (where amount > 0) as contracts_commissioned,
        coalesce(sum(amount) filter (where amount > 0 and not is_advance), 0) as commission_paid,
        coalesce(sum(amount) filter (where amount < 0), 0) as commission_chargebacks,
        coalesce(sum(amount) filter (where amount > 0 and is_advance), 0) as advances_paid,
        coalesce(sum(amount) filter (where amount < 0 and is_advance), 0) as advance_chargebacks,
        sum(amount) as net_commission,
        count(*) as commission_lines
    from {{ ref('fct_commission') }}
    group by 1, 2, 3
),

-- First positive line per agent x contract, and the contract's primary agent.
agent_contract_first as (
    select
        agent_key,
        tenant_key,
        contract_key,
        min(posting_period_key) as posting_period_key,
        row_number() over (partition by contract_key order by sum(amount) desc, agent_key) = 1 as is_primary
    from {{ ref('fct_commission') }}
    where amount > 0
    group by 1, 2, 3
),

first_lines as (
    select
        agent_key,
        tenant_key,
        posting_period_key,
        count(*) as contracts_first_commissioned,
        count(*) filter (where is_primary) as contracts_first_commissioned_primary
    from agent_contract_first
    group by 1, 2, 3
),

advance as (
    select
        agent_key,
        tenant_key,
        contract_key,
        coalesce(sum(amount) filter (where amount > 0), 0) as advance_amount,
        coalesce(sum(amount) filter (where amount < 0), 0) as advance_chargeback,
        min(posting_period_key) filter (where amount < 0) as chargeback_period_key
    from {{ ref('fct_commission') }}
    where is_advance
    group by 1, 2, 3
),

-- Earning down on the contract's commission curve, up to (and including) the chargeback period.
earn_down as (
    select
        a.agent_key,
        a.tenant_key,
        a.contract_key,
        e.posting_period_key,
        a.advance_amount * sum(e.earned_pct) as earned
    from advance as a
    inner join {{ ref('fct_earned_disbursement') }} as e
        on e.contract_key = a.contract_key
        and e.disbursement_type = 'COMMISSION'
        and (a.chargeback_period_key is null or e.posting_period_key <= a.chargeback_period_key)
    group by 1, 2, 3, 4, a.advance_amount
),

-- On cancel: release what is left of the advance after the earning to date.
release as (
    select
        a.agent_key,
        a.tenant_key,
        a.contract_key,
        a.chargeback_period_key as posting_period_key,
        a.advance_amount - coalesce(sum(d.earned), 0) as earned
    from advance as a
    left join earn_down as d using (agent_key, tenant_key, contract_key)
    where a.chargeback_period_key is not null
    group by 1, 2, 3, 4, a.advance_amount
),

recoveries as (
    select agent_key, tenant_key, posting_period_key, sum(earned) as advance_recoveries
    from (select * from earn_down union all select * from release)
    group by 1, 2, 3
),

-- Commission earned on the cost-split curve, split over each contract's agents by their share of the
-- contract's positive ledger lines.
agent_share as (
    select
        agent_key,
        tenant_key,
        contract_key,
        sum(amount) / sum(sum(amount)) over (partition by contract_key) as share
    from {{ ref('fct_commission') }}
    where amount > 0
    group by 1, 2, 3
),

curve as (
    select s.agent_key, s.tenant_key, e.posting_period_key, sum(e.earned_amount * s.share) as earned_on_curve
    from {{ ref('fct_earned_disbursement') }} as e
    inner join agent_share as s using (contract_key)
    where e.disbursement_type = 'COMMISSION'
    group by 1, 2, 3
),

-- Cost-split commission on contracts with no agent-ledger line (the legacy book): one reconciling
-- row per tenant x period x source system, so cost split = agent ledger + this row.
no_ledger_lines as (
    select d.*
    from {{ ref('fct_disbursement') }} as d
    where d.disbursement_type = 'COMMISSION'
        and not exists (select 1 from {{ ref('fct_commission') }} as c where c.contract_key = d.contract_key)
),

no_ledger_split as (
    select
        tenant_key,
        source_system,
        posting_period_key,
        count(distinct contract_key) filter (where amount > 0) as contracts_commissioned,
        coalesce(sum(amount) filter (where amount > 0), 0) as commission_paid,
        coalesce(sum(amount) filter (where amount < 0), 0) as commission_chargebacks,
        sum(amount) as net_commission,
        count(*) as commission_lines
    from no_ledger_lines
    group by 1, 2, 3
),

no_ledger_first as (
    select tenant_key, source_system, posting_period_key, count(*) as contracts_first_commissioned
    from (
        select contract_key, tenant_key, source_system, min(posting_period_key) as posting_period_key
        from no_ledger_lines
        where amount > 0
        group by 1, 2, 3
    )
    group by 1, 2, 3
),

no_ledger_curve as (
    select d.tenant_key, d.source_system, e.posting_period_key, sum(e.earned_amount) as earned_on_curve
    from {{ ref('fct_earned_disbursement') }} as e
    inner join (select distinct contract_key, tenant_key, source_system from no_ledger_lines) as d
        using (contract_key)
    where e.disbursement_type = 'COMMISSION'
    group by 1, 2, 3
),

no_ledger as (
    select
        'NO_AGENT_LEDGER' as agent_key,
        tenant_key,
        source_system,
        posting_period_key,
        coalesce(s.contracts_commissioned, 0) as contracts_commissioned,
        coalesce(nf.contracts_first_commissioned, 0) as contracts_first_commissioned,
        coalesce(nf.contracts_first_commissioned, 0) as contracts_first_commissioned_primary,
        coalesce(s.commission_paid, 0) as commission_paid,
        coalesce(s.commission_chargebacks, 0) as commission_chargebacks,
        0.0 as advances_paid,
        0.0 as advance_chargebacks,
        0.0 as advance_recoveries,
        coalesce(s.net_commission, 0) as net_commission,
        coalesce(s.commission_lines, 0) as commission_lines,
        coalesce(c.earned_on_curve, 0) as earned_on_curve
    from no_ledger_split as s
    full outer join no_ledger_curve as c using (tenant_key, source_system, posting_period_key)
    left join no_ledger_first as nf using (tenant_key, source_system, posting_period_key)
),

agent_rows as (
    select
        agent_key,
        tenant_key,
        'admin_core' as source_system,
        posting_period_key,
        coalesce(l.contracts_commissioned, 0) as contracts_commissioned,
        coalesce(fl.contracts_first_commissioned, 0) as contracts_first_commissioned,
        coalesce(fl.contracts_first_commissioned_primary, 0) as contracts_first_commissioned_primary,
        coalesce(l.commission_paid, 0) as commission_paid,
        coalesce(l.commission_chargebacks, 0) as commission_chargebacks,
        coalesce(l.advances_paid, 0) as advances_paid,
        coalesce(l.advance_chargebacks, 0) as advance_chargebacks,
        round(coalesce(r.advance_recoveries, 0), 2) as advance_recoveries,
        coalesce(l.net_commission, 0) as net_commission,
        coalesce(l.commission_lines, 0) as commission_lines,
        coalesce(c.earned_on_curve, 0) as earned_on_curve
    from lines as l
    full outer join recoveries as r using (agent_key, tenant_key, posting_period_key)
    full outer join curve as c using (agent_key, tenant_key, posting_period_key)
    left join first_lines as fl using (agent_key, tenant_key, posting_period_key)
),

filled as (
    select * from agent_rows
    union all by name
    select * from no_ledger
)

select
    f.agent_key,
    f.agent_key = 'NO_AGENT_LEDGER' as is_no_agent_ledger,
    f.source_system,
    case when f.agent_key = 'NO_AGENT_LEDGER' then 'NO-LEDGER' else a.agent_code end as agent_code,
    case when f.agent_key = 'NO_AGENT_LEDGER' then 'Legacy contracts, no agent ledger' else a.agent_name end
        as agent_name,
    case when f.agent_key = 'NO_AGENT_LEDGER' then 'Reconciling item' else a.territory end as territory,
    f.tenant_key,
    f.posting_period_key,
    p.period_end,
    case when p.is_closed then 'FINAL' else 'PRELIMINARY' end as statement_status,
    f.contracts_commissioned,
    f.contracts_first_commissioned,
    f.contracts_first_commissioned_primary,
    f.commission_lines,
    f.commission_paid,
    f.commission_paid + f.advances_paid as commission_written,
    f.commission_paid + f.advance_recoveries as commission_expensed,
    f.commission_chargebacks,
    f.advances_paid,
    f.advance_recoveries,
    f.advances_paid - f.advance_recoveries as advances_net,
    f.advance_chargebacks,
    f.net_commission,
    {{ safe_divide('-f.commission_chargebacks', 'f.commission_paid + f.advances_paid') }} as chargeback_rate,
    f.earned_on_curve as commission_earned_on_curve,
    f.commission_paid + f.advance_recoveries - f.earned_on_curve as expensed_ahead_of_curve,
    sum(f.advances_paid - f.advance_recoveries) over w as advance_balance,
    sum(f.net_commission) over w as itd_net_commission,
    {{ audit_columns() }}
from filled as f
left join {{ ref('dim_agent') }} as a using (agent_key)
left join {{ ref('dim_posting_period') }} as p using (posting_period_key)
window w as (
    partition by f.agent_key, f.tenant_key, f.source_system order by f.posting_period_key
    rows unbounded preceding
)
