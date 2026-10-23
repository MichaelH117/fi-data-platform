-- Reconciliation: Section 2 line 2.6 (claims paid, all contracts, amount) equals the claim detail
-- tabs (paid in the regulatory year for PERIOD, paid to the period end for ITD), per state and
-- provider. This is the like-for-like dollar figure for the line 2.4 claim count.
with summary as (
    select state, provider_key, scope, claims_paid_amount_all
    from {{ ref('mart_funded_reserve_summary') }}
    where basis = 'COVERAGE_COST'
),

detail as (
    select state, provider_key, 'PERIOD' as scope, sum(coalesce(paid_in_period, 0)) as paid
    from {{ ref('mart_funded_reserve_claims') }}
    where has_requirement and is_filing_product_scope
    group by all
    union all
    select state, provider_key, 'ITD', sum(coalesce(paid_itd, 0))
    from {{ ref('mart_funded_reserve_claims') }}
    where has_requirement and is_filing_product_scope
    group by all
)

select s.state, s.provider_key, s.scope, s.claims_paid_amount_all, d.paid as detail_paid
from summary as s
left join detail as d using (state, provider_key, scope)
where abs(s.claims_paid_amount_all - coalesce(d.paid, 0)) > 0.01
