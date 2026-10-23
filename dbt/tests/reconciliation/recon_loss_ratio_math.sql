-- Loss ratio = incurred / EARNED, recomputed independently from fct_contract_monthly for every
-- dealer-month of the (deprecated) V1 dealer mart's trailing-12 window.
with recomputed as (
    select
        mo.dealer_code,
        mo.month_start,
        sum(m.incurred_claims) / nullif(sum(m.earned_premium), 0) as t12_loss_ratio
    from {{ ref('mart_dealer_loss_ratio') }} as mo
    inner join {{ ref('fct_contract_monthly') }} as m
        on m.dealer_code = mo.dealer_code
        and m.month_start > mo.month_start - interval 12 month
        and m.month_start <= mo.month_start
    group by all
)

select mo.dealer_code, mo.month_start, mo.t12_loss_ratio, r.t12_loss_ratio as expected
from {{ ref('mart_dealer_loss_ratio') }} as mo
left join recomputed as r using (dealer_code, month_start)
where abs(coalesce(mo.t12_loss_ratio, -1) - coalesce(r.t12_loss_ratio, -1)) > 1e-6
