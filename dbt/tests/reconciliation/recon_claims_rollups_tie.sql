-- Every claim roll-up is derived from the one claims fact and ties back to it to the cent.
with totals as (
    select 'claim summary paid = fact paid (contract-keyed lines)' as check_name,
        (select sum(paid_amount) from {{ ref('fct_claim_payment_line') }} where contract_key <> '-1') as expected,
        (select sum(paid_to_date) from {{ ref('dim_contract_claim_summary') }}) as actual
    union all
    select 'contract monthly incurred = fact incurred (contracts with an earning window)',
        (select sum(f.paid_amount + f.pending_amount)
         from {{ ref('fct_claim_payment_line') }} as f
         where f.contract_key in (select contract_key from {{ ref('int_contract_months') }})),
        (select sum(incurred_claims) from {{ ref('fct_contract_monthly') }})
    union all
    select 'V1 claims trend paid = V1 claim paid',
        (select sum(paid_amount) from {{ ref('fct_claim') }}),
        (select sum(paid_amount) from {{ ref('mart_claims_trend') }})
    union all
    select 'V1 dealer mart earned = contract monthly earned',
        (select sum(earned_premium) from {{ ref('fct_contract_monthly') }}),
        (select sum(earned_premium) from {{ ref('mart_dealer_loss_ratio') }})
)

select * from totals
where abs(coalesce(expected, 0) - coalesce(actual, 0)) > 1.00
