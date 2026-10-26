-- V6 edit 2: the reserve bridge closes to the cent, and its two ends are the two reports a reinsurer
-- compares. Fails on any month where
--   (a) the steps do not walk gross new reserve to net written by effective month (residual <> 0), or
--   (b) step 1 differs from the New Reserves mart (mart_new_reserves_by_term), or
--   (c) net written by entered month differs from the Experience mart (mart_experience).
with nr as (
    select tenant_key, entered_month as month_start, sum(new_reserve) as new_reserve
    from {{ ref('mart_new_reserves_by_term') }}
    group by 1, 2
),

ex as (
    select tenant_key, entered_month as month_start, sum(net_written) as net_written,
        sum(contracts_net) as contracts_net
    from {{ ref('mart_experience') }}
    group by 1, 2
)

select b.tenant_key, b.month_start, b.residual, b.gross_new_reserve, nr.new_reserve,
    b.net_written_entered, ex.net_written, b.contracts_net, ex.contracts_net as experience_contracts_net
from {{ ref('mart_reserve_bridge') }} as b
left join nr using (tenant_key, month_start)
left join ex using (tenant_key, month_start)
where abs(b.residual) > 0.005
    or abs(b.gross_new_reserve - coalesce(nr.new_reserve, 0)) > 0.005
    or abs(b.net_written_entered - coalesce(ex.net_written, 0)) > 0.005
    or b.contracts_net <> coalesce(ex.contracts_net, 0)
