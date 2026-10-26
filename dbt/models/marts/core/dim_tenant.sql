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
    -- V7 edit 45: the id each tenant's own admin instance gives itself. Every instance calls itself
    -- company 1, so the id is unique only together with tenant_key; never join on it alone.
    cast({{ column_or(ref('stg_admin_core__administrator'), 'admin_company_id', 'null') }} as bigint) as admin_company_id,
    {{ audit_columns() }}
from {{ ref('stg_admin_core__administrator') }}
where not is_deleted and tenant_id <> 0
