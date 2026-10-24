-- Dealer participation structures (dealer-owned obligor, CFC, NCFC, PORC, retro, profit share)
-- with their reinsurer. Only reportable dealers.
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
    {{ audit_columns() }}
from {{ ref('stg_admin_core__participation_entity') }} as e
inner join {{ ref('stg_admin_core__dealer') }} as d using (dealer_id)
inner join {{ ref('int_reportable_dealers') }} as rd on rd.dealer_number = d.dealer_number and rd.is_reportable
inner join {{ ref('int_dealer_survivorship') }} as dm on dm.source_dealer_number = d.dealer_number
left join {{ ref('stg_admin_core__reinsurer') }} as r using (reinsurer_id)
where not e.is_deleted
