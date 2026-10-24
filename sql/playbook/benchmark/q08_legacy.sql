-- database: legacy
-- timeout: 60s
-- expect_error: does not exist
-- question: In-force contract count for Tenant B, first try: the main-schema query pointed at the mirror.
-- trap:     schema drift: the mirror renamed the status column, so the copied query errors out.
set statement_timeout = '60s';

select count(*) as in_force_contracts from tenant_b.vw_dim_contracts where "CurrentContractStatus" = 'INFORCE';
