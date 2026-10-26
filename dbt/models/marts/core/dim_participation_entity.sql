-- Dealer participation structures (dealer-owned obligor, CFC, NCFC, PORC, retro, profit share)
-- with their reinsurer. Only reportable dealers.
-- V6 edit 13: run-off and transfer. status ACTIVE | RUNOFF, runoff_date (after it the entity takes no
-- new contracts: their reserve stays with the obligor), cession_type (1-char code) and gl_class_code,
-- for the entity and for its reinsurer. Program-type grain: dim_participation_entity_program.
select
    md5('participation|' || cast(e.entity_id as varchar)) as participation_entity_key,
    e.entity_id,
    e.entity_name,
    e.structure_type as entity_type,
    dm.dealer_key,  -- surviving dealer (int_dealer_survivorship)
    d.state,
    e.tenant_id as tenant_key,
    r.reinsurer_name,
    e.effective_date,
    e.profit_share_pct,
    coalesce(e.status, 'ACTIVE') as status,
    cast(e.runoff_date as date) as runoff_date,
    e.cession_type,
    e.gl_class_code,
    coalesce(r.status, 'ACTIVE') as reinsurer_status,
    cast(r.runoff_date as date) as reinsurer_runoff_date,
    r.cession_type as reinsurer_cession_type,
    r.gl_class_code as reinsurer_gl_class_code,
    {{ audit_columns() }}
from {{ ref('stg_admin_core__participation_entity') }} as e
inner join {{ ref('stg_admin_core__dealer') }} as d using (dealer_id)
inner join {{ ref('int_reportable_dealers') }} as rd on rd.dealer_number = d.dealer_number and rd.is_reportable
inner join {{ ref('int_dealer_survivorship') }} as dm on dm.source_dealer_number = d.dealer_number
left join {{ ref('stg_admin_core__reinsurer') }} as r using (reinsurer_id)
where not e.is_deleted
