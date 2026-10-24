-- database: modern
-- timeout: 60s
-- question: In-force contract count for Tenant B on the modern star schema.
-- fix:      tenant is a column (tenant_key) on one set of tables, protected by row-level security, so there is
--           no mirror to lag and no copy whose columns can drift.
set statement_timeout = '60s';

select count(*) as in_force_contracts, max(entered_date) as newest_contract
from core.dim_contract
where tenant_key = 2 and status = 'INFORCE';
