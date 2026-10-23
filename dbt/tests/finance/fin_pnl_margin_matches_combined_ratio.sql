-- P&L bridge: program margin and the combined ratio treat the admin fee the same way (a program cost),
-- so program_margin / earned_premium = 1 - combined_ratio on every row and on the total.
with rows_ as (
    select tenant_key, product_code, posting_period_key, earned_premium, program_margin, combined_ratio
    from {{ ref('mart_pnl_by_product') }}
),

total as (
    select
        sum(earned_premium) as earned_premium,
        sum(program_margin) as program_margin,
        sum(incurred_claims + earned_acquisition_cost + earned_admin_fee) / nullif(sum(earned_premium), 0)
            as combined_ratio
    from {{ ref('mart_pnl_by_product') }}
)

select 'row' as level, tenant_key, product_code, posting_period_key, earned_premium, program_margin,
    combined_ratio
from rows_
where earned_premium <> 0
    and abs(program_margin - (1 - combined_ratio) * earned_premium) > 0.01
union all
select 'total', null, null, null, earned_premium, program_margin, combined_ratio
from total
where earned_premium <> 0
    and abs(program_margin - (1 - combined_ratio) * earned_premium) > 0.01
