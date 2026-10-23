-- Commission statement contract counts must not run ahead of contracts sold. Two checks:
-- 1. per posting period (all tenants), contracts first commissioned (primary agent, so each contract counts
--    once) <= contracts sold (SALE) + reinstatements (a reinstated contract may be re-commissioned);
-- 2. per tenant, the ITD sum of contracts_first_commissioned_primary on agent rows = distinct contracts
--    with a positive agent-ledger line (no contract counted twice across agents or periods).
with stmt as (
    select posting_period_key, sum(contracts_first_commissioned_primary) as contracts
    from {{ ref('mart_commission_statement') }}
    group by 1
),

sold as (
    select
        posting_period_key,
        count(distinct contract_key) filter (where txn_type = 'SALE') as sales,
        count(*) filter (where txn_type = 'REINSTATE') as reinstatements
    from {{ ref('fct_transaction') }}
    group by 1
),

by_period as (
    select 'period' as check_name, null::bigint as tenant_key, posting_period_key, s.contracts,
        coalesce(t.sales, 0) + coalesce(t.reinstatements, 0) as allowed
    from stmt as s
    left join sold as t using (posting_period_key)
    where s.contracts > coalesce(t.sales, 0) + coalesce(t.reinstatements, 0)
),

itd_stmt as (
    select tenant_key, sum(contracts_first_commissioned_primary) as contracts
    from {{ ref('mart_commission_statement') }}
    where not is_no_agent_ledger
    group by 1
),

itd_ledger as (
    select tenant_key, count(distinct contract_key) as contracts
    from {{ ref('fct_commission') }}
    where amount > 0
    group by 1
),

itd as (
    select 'itd_distinct' as check_name, tenant_key, null::integer as posting_period_key, s.contracts,
        l.contracts as allowed
    from itd_stmt as s
    full outer join itd_ledger as l using (tenant_key)
    where coalesce(s.contracts, 0) <> coalesce(l.contracts, 0)
)

select * from by_period
union all
select * from itd
