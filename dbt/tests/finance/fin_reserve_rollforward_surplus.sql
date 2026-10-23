-- Book-wide: at the as-of period, closing funded - closing unearned reserve = ITD earned claims
-- reserve - ITD claims paid - surplus released by profit-share distributions, each recomputed from
-- the core facts and the cession mart. Also, the surplus spread over tenant x category sums to what
-- the cession mart released (no dollar lost in the allocation).
with rf as (
    select
        sum(closing_funded_reserve - closing_unearned_reserve) as rf_surplus,
        (select sum(surplus_released) from {{ ref('mart_reserve_rollforward') }}) as rf_released
    from {{ ref('mart_reserve_rollforward') }}
    where posting_period_key = (select max(posting_period_key) from {{ ref('mart_reserve_rollforward') }})
),

f as (
    select
        (select sum(earned_reserve) from {{ ref('fct_earned_disbursement') }}
            where disbursement_type = 'CLAIMS_RESERVE') as earned,
        (select sum(paid_amount) from {{ ref('fct_claim_payment_line') }} where product_key is not null) as paid,
        (select sum(claims_reserve_released) from {{ ref('mart_cession_experience') }}) as released
)

select rf.rf_surplus, f.earned - f.paid - f.released as expected, rf.rf_released, f.released
from rf cross join f
where abs(rf.rf_surplus - (f.earned - f.paid - f.released)) > 0.05
    or abs(rf.rf_released - f.released) > 0.05
