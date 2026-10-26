-- Selling agents (Admin Core is the master; legacy producer codes resolve to the same agent_code).
-- V6 edit 12: the District > Regional > National roll-up. Admin Core keeps one agent record per
-- agency, so sub_agent is the agent as the commission report lists it ("<code> - <name>"),
-- district / region come from seeds/core/agent_territory_hierarchy.csv (an unmapped territory
-- rolls up to district = territory, region 'UNASSIGNED'), and agent_category is ADVANCE for an agent
-- on an advance agreement, else STANDARD.
-- V7 edit 24: agency_name / principal_name / is_house_agent for the agency roll-up page.
select
    md5('agent|' || cast(a.agent_id as varchar)) as agent_key,
    a.agent_id,
    a.agent_code,
    a.agent_name,
    a.territory,
    a.tenant_id as tenant_key,
    coalesce(a.is_advance_agreement, 0) = 1 as has_advance_agreement,
    a.agent_code || ' - ' || a.agent_name as sub_agent,
    case when coalesce(a.is_advance_agreement, 0) = 1 then 'ADVANCE' else 'STANDARD' end as agent_category,
    coalesce(h.district, a.territory, 'UNASSIGNED') as district,
    coalesce(h.region, 'UNASSIGNED') as region,
    -- V7 edit 24 (column contract with the generator: raw agent.agency_name, principal_name,
    -- is_house_agent). Admin Core keeps one agent record per agency, so until the generator sends
    -- agency_name the agency is the agent itself; principal is unknown; no agent is a house agent.
    coalesce({{ column_or(ref('stg_admin_core__agent'), 'agency_name', 'cast(null as varchar)', 'a') }}, a.agent_name) as agency_name,
    {{ column_or(ref('stg_admin_core__agent'), 'principal_name', 'cast(null as varchar)', 'a') }} as principal_name,
    coalesce(cast({{ column_or(ref('stg_admin_core__agent'), 'is_house_agent', 'cast(null as integer)', 'a') }} as integer), 0) = 1
        as is_house_agent,
    {{ audit_columns() }}
from {{ ref('stg_admin_core__agent') }} as a
left join {{ ref('agent_territory_hierarchy') }} as h on h.territory = a.territory
where not a.is_deleted

union all

select '-1', null, 'UNKNOWN', 'Unknown agent', null, null, false, 'UNKNOWN', 'STANDARD', 'UNASSIGNED',
    'UNASSIGNED', 'Unknown agency', null, false, current_timestamp
