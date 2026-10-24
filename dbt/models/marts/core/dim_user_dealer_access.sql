-- Row-level security map: portal user -> (tenant, dealer). The ONE map behind the semantic RLS
-- table, the per-dealer publish packets and the raw-data portal. Reportable dealers only.
select
    md5('portal_user|' || cast(u.portal_user_id as varchar)) as user_key,
    u.login as user_login,
    u.role,
    u.tenant_id as tenant_key,
    t.tenant_code,
    d.dealer_key,
    d.dealer_number,
    {{ audit_columns() }}
from {{ ref('stg_admin_core__portal_user') }} as u
inner join {{ ref('stg_admin_core__dealer') }} as sd on sd.dealer_id = u.dealer_id
inner join {{ ref('int_dealer_survivorship') }} as dm on dm.source_dealer_number = sd.dealer_number
inner join {{ ref('dim_dealer') }} as d on d.dealer_key = dm.dealer_key and d.tenant_key = u.tenant_id
inner join {{ ref('dim_tenant') }} as t on t.tenant_key = u.tenant_id
where not u.is_deleted
