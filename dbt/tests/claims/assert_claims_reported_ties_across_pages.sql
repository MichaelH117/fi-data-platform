-- Cross-page tie: 'Claims reported' on the Claims KPI page (mart_claims_kpi) and on the Claims & IBNR
-- page (gold.mart_claims_trend) are both by LOSS month. The only allowed difference is the claims
-- with no matched contract (or a loss before the sale), which mart_claims_kpi carries in
-- claims_unmatched. Checked per loss month.
with kpi as (
    select month_start, sum(claims_reported) as reported, sum(claims_unmatched) as unmatched
    from {{ ref('mart_claims_kpi') }} group by 1
),

v1 as (
    select loss_month as month_start, sum(claims_reported) as reported
    from {{ ref('mart_claims_trend') }} group by 1
)

select coalesce(k.month_start, v.month_start) as month_start, k.reported, k.unmatched, v.reported as v1_reported
from kpi as k
full outer join v1 as v using (month_start)
where coalesce(k.reported, 0) - coalesce(k.unmatched, 0) <> coalesce(v.reported, 0)
