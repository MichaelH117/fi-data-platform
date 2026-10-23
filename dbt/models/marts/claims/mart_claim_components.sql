-- Claim components. Grain: component_code x status_group x loss month.
-- Admin Core claims carry itemized components (parts + labor); converted legacy claims only carry a
-- repair category, which is used as a single component with the claim's incurred amount.
with admin_components as (
    select
        b.claim_key,
        upper(cc.component_code) as component_code,
        'claim_component' as component_source,
        coalesce(cc.parts_amount, 0) as parts_amount,
        coalesce(cc.labor_amount, 0) as labor_amount,
        coalesce(cc.labor_hours, 0) as labor_hours
    from {{ ref('stg_admin_core__claim_component') }} as cc
    inner join {{ ref('int_claim_number_bridge') }} as b on b.admin_claim_id = cc.claim_id
    where not coalesce(cc.is_deleted, false)
),

fallback as (
    select
        s.claim_key,
        s.top_component as component_code,
        'repair_category' as component_source,
        s.incurred_amount as parts_amount,
        0.0 as labor_amount,
        0.0 as labor_hours
    from {{ ref('mart_claim_summary') }} as s
    where s.component_count = 0
),

all_components as (
    select * from admin_components
    union all
    select * from fallback
)

select
    a.component_code,
    s.status_group,
    s.loss_month as month_start,
    {{ yyyymm('s.loss_month') }} as loss_month_key,
    string_agg(distinct a.component_source, ',' order by a.component_source) as component_sources,
    count(distinct a.claim_key) as claims,
    count(*) as component_lines,
    round(sum(a.parts_amount), 2) as parts_amount,
    round(sum(a.labor_amount), 2) as labor_amount,
    round(sum(a.labor_hours), 1) as labor_hours,
    round(sum(a.parts_amount + a.labor_amount), 2) as component_cost,
    {{ audit_columns() }}
from all_components as a
inner join {{ ref('mart_claim_summary') }} as s on s.claim_key = a.claim_key
group by all
