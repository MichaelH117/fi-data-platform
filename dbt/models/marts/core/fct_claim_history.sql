-- Grain: one claim status change (Admin Core claim_history). Legacy claims kept no change log;
-- their reconstructed status intervals live in int_status_history_backfill.
select
    md5('admin_core|claim_history|' || cast(h.claim_history_id as varchar)) as claim_history_key,
    c.claim_key,
    c.contract_key,
    coalesce(k.dealer_key, '-1') as dealer_key,
    coalesce(k.product_key, '-1') as product_key,
    c.tenant_key,
    h.changed_at,
    {{ date_key('h.changed_at') }} as changed_date_key,
    {{ posting_period_key('h.changed_at', 'pp') }} as posting_period_key,
    {{ period_start_date(posting_period_key('h.changed_at', 'pp')) }} as posting_period_start_date,
    h.from_status,
    h.to_status,
    coalesce(h.pre_deductible_change, 0) as pre_deductible_change,
    coalesce(h.post_deductible_change, 0) as post_deductible_change,
    coalesce(h.collected_deductible, 0) as collected_deductible,
    {{ audit_columns() }}
from {{ ref('stg_admin_core__claim_history') }} as h
inner join {{ ref('int_claim_number_bridge') }} as b
    on b.admin_claim_id = h.claim_id and b.source_system = 'admin_core'
inner join {{ ref('dim_claim') }} as c on c.claim_key = b.claim_key
left join {{ ref('dim_contract') }} as k on k.contract_key = c.contract_key
{{ posting_period_join('h.created_at', 'pp') }}
where not h.is_deleted
