-- Claim components. Grain: component_code x status_group x loss month.
-- Aggregates mart_claim_component_lines (itemized Admin Core components; legacy repair category as
-- a single component with the claim's incurred amount), so the row-level export ties to this mart.
select
    component_code,
    status_group,
    loss_month as month_start,
    {{ yyyymm('loss_month') }} as loss_month_key,
    string_agg(distinct component_source, ',' order by component_source) as component_sources,
    count(distinct claim_key) as claims,
    count(*) as component_lines,
    round(sum(parts_amount), 2) as parts_amount,
    round(sum(labor_amount), 2) as labor_amount,
    round(sum(labor_hours), 1) as labor_hours,
    round(sum(parts_amount + labor_amount), 2) as component_cost,
    {{ audit_columns() }}
from {{ ref('mart_claim_component_lines') }}
group by all
