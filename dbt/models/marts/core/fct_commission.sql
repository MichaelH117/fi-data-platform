-- Grain: one commission line (agent x contract x transaction). Advances flagged.
select
    md5('admin_core|commission|' || cast(m.commission_id as varchar)) as commission_key,
    m.commission_id,
    k.contract_key,
    k.dealer_key,
    k.product_key,
    k.tenant_key,
    coalesce(a.agent_key, '-1') as agent_key,
    m.posted_at,
    {{ date_key('m.posted_at') }} as posted_date_key,
    {{ posting_period_key('m.posted_at', 'pp') }} as posting_period_key,
    {{ period_start_date(posting_period_key('m.posted_at', 'pp')) }} as posting_period_start_date,
    m.amount,
    m.share_pct,
    coalesce(m.is_advance, 0) = 1 as is_advance,
    {{ audit_columns() }}
from {{ ref('stg_admin_core__commission') }} as m
inner join {{ ref('dim_contract') }} as k
    on k.contract_key = {{ contract_key("'admin_core'", 'm.contract_id') }}
left join {{ ref('dim_agent') }} as a on a.agent_id = m.agent_id
{{ posting_period_join('m.created_at', 'pp') }}
where not m.is_deleted
