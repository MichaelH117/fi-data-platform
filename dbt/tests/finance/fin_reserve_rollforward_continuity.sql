-- Roll-forward continuity: each month opens at last month's closing balance, and the first month
-- opens at zero.
with r as (
    select
        *,
        lag(closing_funded_reserve) over w as prior_funded,
        lag(closing_unearned_reserve) over w as prior_unearned
    from {{ ref('mart_reserve_rollforward') }}
    window w as (partition by tenant_key, category_code order by posting_period_key)
)

select tenant_key, category_code, posting_period_key, opening_funded_reserve, prior_funded
from r
where abs(opening_funded_reserve - coalesce(prior_funded, 0)) > 0.01
    or abs(opening_unearned_reserve - coalesce(prior_unearned, 0)) > 0.01
