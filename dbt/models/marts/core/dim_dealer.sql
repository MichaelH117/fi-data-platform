-- Dealers: the CURRENT row of snap_dealer (history lives in the snapshot), plus legacy-only
-- dealer codes and the unknown member '-1'. Demo / training dealers are excluded
-- (int_reportable_dealers). dealer_key = md5('dealer|' || dealer_number) in both systems.
-- Who carries the dealer's risk comes from the participation-entity dimension that finance's
-- cession / experience mart uses (dim_participation_entity, keyed by dealer_key):
-- participation_entity_key / _name / _structure and a display participation_label ('<entity>, <structure>'), which reads
-- 'no dealer participation' when the dealer has none (never NULL). reinsurance_company is the
-- LEGACY admin system's free-text attribute, kept for lineage only; do not show it as the risk carrier.
with current_dealers as (
    select *
    from {{ ref('snap_dealer') }}
    where dbt_valid_to is null
),

legacy_dealers as (
    select * from {{ ref('stg_legacy_admin__dealers') }} where not is_deleted
),

reportable as (
    select dealer_number from {{ ref('int_reportable_dealers') }} where is_reportable
),

participation as (
    select dealer_key, participation_entity_key, entity_name, entity_type
    from {{ ref('dim_participation_entity') }}
    qualify row_number() over (partition by dealer_key order by effective_date desc, entity_id desc) = 1
),

dealers as (
select
    md5('dealer|' || d.dealer_number) as dealer_key,
    d.dealer_id,
    d.dealer_number,
    d.dealer_name,
    d.city,
    d.state,
    lower(d.status) as dealer_status,
    d.onboarded_date,
    coalesce(md5('dealer_group|' || cast(d.dealer_group_id as varchar)), '-1') as dealer_group_key,
    coalesce(md5('agent|' || cast(d.agent_id as varchar)), '-1') as agent_key,
    d.tenant_id as tenant_key,
    d.remit_rule,
    coalesce(d.weekly_remit_flag, 0) = 1 as is_weekly_remit,
    d.program,
    l.reinsurance_company,
    'admin_core' as source_system,
    d.dbt_valid_from as valid_from,
    true as is_current,
    {{ audit_columns() }}
from current_dealers as d
inner join reportable as r using (dealer_number)
left join legacy_dealers as l on l.dealer_code = d.dealer_number

union all

select
    md5('dealer|' || l.dealer_code),
    null,
    l.dealer_code,
    l.dealer_name,
    l.city,
    l.state,
    l.dealer_status,
    l.onboarded_date,
    '-1',
    '-1',
    1,
    null,
    false,
    null,
    l.reinsurance_company,
    'legacy_admin',
    l.created_at,
    true,
    current_timestamp
from legacy_dealers as l
inner join reportable as r on r.dealer_number = l.dealer_code
where l.dealer_code not in (select dealer_number from current_dealers)

union all

select
    '-1', null, 'UNKNOWN', 'Unknown dealer', null, null, 'unknown', null, '-1', '-1', null, null,
    false, null, null, null, timestamp '1900-01-01', true, current_timestamp
)

select
    d.* exclude (_built_at),
    p.participation_entity_key,
    p.entity_name as participation_entity_name,
    p.entity_type as participation_structure,
    case
        when p.participation_entity_key is null then 'no dealer participation'
        else p.entity_name || ', ' || case p.entity_type
            when 'DEALER_OWNED_OBLIGOR' then 'dealer-owned obligor'
            when 'CFC' then 'controlled foreign reinsurer'
            when 'NCFC' then 'non-controlled foreign reinsurer'
            when 'PORC' then 'producer-owned reinsurer'
            when 'RETRO' then 'retrospective arrangement'
            when 'PROFIT_SHARE' then 'profit share (obligor holds the reserve)'
            else lower(p.entity_type)
        end
    end as participation_label,
    d._built_at
from dealers as d
left join participation as p on p.dealer_key = d.dealer_key and d.dealer_key <> '-1'
