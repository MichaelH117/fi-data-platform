-- mart_dealer_scorecard must tie out to its sources in total (unknown dealer '-1' excluded on both
-- sides, months up to the as-of month): production to mart_dealer_production, earned/incurred to
-- fct_contract_monthly, claims to mart_claim_summary, remittance and late fees to fct_cash_transaction.
with m as (select cast(date_trunc('month', as_of_date) as date) as as_of_month from {{ ref('dim_reporting_date') }}),

card as (
    select sum(contracts_written) as written, sum(coverage_cost) as coverage, sum(earned_claims_reserve) as earned,
        sum(incurred_claims) as incurred, sum(claims_reported) as claims, sum(remittances) as remits,
        sum(late_fees) as late_fees
    from {{ ref('mart_dealer_scorecard') }}
),

src as (
    select
        (select sum(contracts_written) from {{ ref('mart_dealer_production') }}, m
         where dealer_key <> '-1' and month_start <= as_of_month) as written,
        (select sum(coverage_cost) from {{ ref('mart_dealer_production') }}, m
         where dealer_key <> '-1' and month_start <= as_of_month) as coverage,
        (select sum(earned_premium) from {{ ref('fct_contract_monthly') }}, m
         where dealer_key <> '-1' and month_start <= as_of_month) as earned,
        (select sum(incurred_claims) from {{ ref('fct_contract_monthly') }}, m
         where dealer_key <> '-1' and month_start <= as_of_month) as incurred,
        (select count(*) from {{ ref('mart_claim_summary') }}, m
         where dealer_key <> '-1' and loss_month <= as_of_month) as claims,
        (select count(*) from {{ ref('fct_cash_transaction') }}, m
         where dealer_key <> '-1' and posting_period_key <= {{ yyyymm('as_of_month') }}) as remits,
        (select sum(late_fee) from {{ ref('fct_cash_transaction') }}, m
         where dealer_key <> '-1' and posting_period_key <= {{ yyyymm('as_of_month') }}) as late_fees
),

checks as (
    select 'written' as check_name, c.written::double as card_value, s.written::double as source_value, 0.0 as tol from card c, src s
    union all select 'coverage_cost', c.coverage, s.coverage, 1.0 from card c, src s
    union all select 'earned_claims_reserve', c.earned, s.earned, 1.0 from card c, src s
    union all select 'incurred_claims', c.incurred, s.incurred, 1.0 from card c, src s
    union all select 'claims_reported', c.claims, s.claims, 0.0 from card c, src s
    union all select 'remittances', c.remits, s.remits, 0.0 from card c, src s
    union all select 'late_fees', c.late_fees, s.late_fees, 1.0 from card c, src s
)

select * from checks where abs(coalesce(card_value, 0) - coalesce(source_value, 0)) > tol
