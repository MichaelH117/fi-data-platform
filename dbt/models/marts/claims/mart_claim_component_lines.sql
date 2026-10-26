-- Row-level claim components (V6 edit 18: the "claim components" export). Grain: one component
-- line. Admin Core claims carry itemized components (parts + labor); converted legacy claims only
-- carry a repair category, used as one component line with the claim's incurred amount.
-- mart_claim_components aggregates THIS model, so the export and the page always agree.
with admin_components as (
    select
        b.claim_key,
        cast(cc.claim_component_id as varchar) as component_line_id,
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
        'RC-' || s.claim_key as component_line_id,
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
    a.component_line_id,
    a.claim_key,
    s.claim_number,
    s.contract_number,
    s.source_system,
    s.status_group,
    s.loss_month,
    a.component_code,
    a.component_source,
    round(a.parts_amount, 2) as parts_amount,
    round(a.labor_amount, 2) as labor_amount,
    round(a.labor_hours, 1) as labor_hours,
    round(a.parts_amount + a.labor_amount, 2) as component_cost,
    {{ audit_columns() }}
from all_components as a
inner join {{ ref('mart_claim_summary') }} as s on s.claim_key = a.claim_key
