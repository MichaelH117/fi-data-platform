-- Diagnostic (V7 edit 26): add-on contracts point to the service contract they were sold with.
-- The measured value (pipeline diagnostics) is how many tie-back values name no contract (junk
-- text, or a contract outside the reportable book); that count never fails. The check fails when a
-- resolved link crosses tenants: an id is only unique within its tenant (edit 45), so a link to
-- another tenant's contract means the parse or the join lost the tenant.
select c.contract_key, c.tenant_key, c.parent_contract_key, p.tenant_key as parent_tenant_key
from {{ ref('dim_contract') }} as c
inner join {{ ref('dim_contract') }} as p on p.contract_key = c.parent_contract_key
where c.tenant_key <> p.tenant_key
