-- White-label administrators (tenants) and their branding, used by reports, dealer packets and
-- the semantic RLS table. tenant_key = Admin Core tenant_id. tenant_id 0 (shared catalog rows) is
-- not a tenant.
select
    tenant_id as tenant_key,
    administrator_code as tenant_code,
    reportable_name,
    logo_path,
    primary_color,
    secondary_color,
    {{ audit_columns() }}
from {{ ref('stg_admin_core__administrator') }}
where not is_deleted and tenant_id <> 0
