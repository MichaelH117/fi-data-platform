-- 'Open claims aged 30+' in mart_claims_kpi must equal core dim_claim.aging_30_plus counted over
-- the non-closed (open) claims, so the KPI and the core flag never drift apart.
select k.n as kpi_aged, c.n as core_aged
from (select sum(open_claims_aged_30_plus) as n from {{ ref('mart_claims_kpi') }}) as k
cross join (
    select count(*) as n from {{ ref('dim_claim') }} where status_group = 'open' and aging_30_plus
) as c
where coalesce(k.n, 0) <> coalesce(c.n, 0)
