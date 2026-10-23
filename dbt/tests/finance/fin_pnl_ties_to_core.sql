-- Control totals: the P&L must add up to the core facts it is built from (check row counts and
-- control totals on every pull). One row per line that does not tie within $0.01.
-- Net written ties to the transaction ledger (SALE + AMEND + CANCEL + REINSTATE), the unmatched-claims
-- reconciling line ties to the claim lines with no contract key, and dealer participation ties to the
-- profit-share statement's period movement.
with mart as (
    select
        sum(earned_premium) as earned_premium,
        sum(incurred_claims) as incurred_claims,
        sum(written_coverage_cost) as written_coverage_cost,
        sum(coverage_cost_refunded) as coverage_cost_refunded,
        sum(coverage_cost_reinstated) as coverage_cost_reinstated,
        sum(net_written_coverage_cost) as net_written_coverage_cost,
        sum(contracts_written) as contracts_written,
        sum(incurred_claims) filter (where is_unmatched_claims) as unmatched_incurred,
        sum(claim_lines) filter (where is_unmatched_claims) as unmatched_lines,
        sum(dealer_participation) as dealer_participation
    from {{ ref('mart_pnl_by_product') }}
),

core as (
    select
        (select sum(earned_amount) from {{ ref('fct_earned_disbursement') }}) as earned_premium,
        (select sum(incurred_change) from {{ ref('fct_claim_payment_line') }}) as incurred_claims,
        (select sum(coverage_cost) from {{ ref('fct_gross_premium') }}) as written_coverage_cost,
        (select coalesce(sum(coverage_cost_refund), 0) from {{ ref('fct_cancellation') }}) as coverage_cost_refunded,
        (
            select coalesce(sum(reversal_amount), 0) from {{ ref('fct_reinstatement') }}
            where status = 'APPROVED'
        ) as coverage_cost_reinstated,
        (
            select coalesce(sum(amount), 0) from {{ ref('fct_transaction') }}
            where txn_type in ('SALE', 'AMEND', 'CANCEL', 'REINSTATE')
        ) as net_written_coverage_cost,
        (select sum(contracts_written) from {{ ref('fct_gross_premium') }}) as contracts_written,
        (
            select coalesce(sum(incurred_change), 0) from {{ ref('fct_claim_payment_line') }}
            where contract_key::varchar = '-1'
        ) as unmatched_incurred,
        (
            select count(*) from {{ ref('fct_claim_payment_line') }}
            where contract_key::varchar = '-1'
        ) as unmatched_lines,
        (select coalesce(sum(period_profit_share), 0) from {{ ref('mart_profit_share_statement') }})
            as dealer_participation
),

lines as (
    select 'earned_premium' as line, m.earned_premium as mart_value, c.earned_premium as core_value
    from mart as m, core as c
    union all
    select 'incurred_claims', m.incurred_claims, c.incurred_claims from mart as m, core as c
    union all
    select 'written_coverage_cost', m.written_coverage_cost, c.written_coverage_cost from mart as m, core as c
    union all
    select 'coverage_cost_refunded', m.coverage_cost_refunded, c.coverage_cost_refunded from mart as m, core as c
    union all
    select 'coverage_cost_reinstated', m.coverage_cost_reinstated, c.coverage_cost_reinstated
    from mart as m, core as c
    union all
    select 'net_written_vs_transactions', m.net_written_coverage_cost, c.net_written_coverage_cost
    from mart as m, core as c
    union all
    select 'contracts_written', m.contracts_written, c.contracts_written from mart as m, core as c
    union all
    select 'unmatched_claims_incurred', m.unmatched_incurred, c.unmatched_incurred from mart as m, core as c
    union all
    select 'unmatched_claim_lines', m.unmatched_lines, c.unmatched_lines from mart as m, core as c
    union all
    select 'dealer_participation', m.dealer_participation, c.dealer_participation from mart as m, core as c
)

select * from lines
where abs(coalesce(mart_value, 0) - coalesce(core_value, 0)) > 0.01
