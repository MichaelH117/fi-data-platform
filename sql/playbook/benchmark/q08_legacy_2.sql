-- database: legacy
-- timeout: 60s
-- question: In-force contract count for Tenant B, second try: the query rewritten for the mirror's columns.
-- trap:     Tenant B is a full MIRROR schema copied at an earlier cutoff, so it lags the live book, and its
--           status column was renamed in the copy (a query written against the main schema fails there).
set statement_timeout = '60s';

select count(*) as in_force_contracts, max("EnteredDate") as newest_contract_in_mirror
from tenant_b.vw_dim_contracts
where "ContractStatus" = 'INFORCE';
