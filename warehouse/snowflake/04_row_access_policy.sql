-- =====================================================================================================
-- 04  Row access policies: a reader sees only its own tenant and dealer rows
-- Written for Snowflake, NOT executed by this repo. Row access policies need Enterprise edition.
--
-- Same rule as the semantic model's dynamic RLS table and the per-dealer publish packets:
--   one access map (user -> tenant, dealer)  ->  every reader is filtered the same way.
--   dealer_key NULL in the map = every dealer in that tenant (a white-label administrator's staff).
-- TRANSFORMER (dbt, tests, reconciliation) and SYSADMIN see every row.
-- Mart key columns are VARCHAR md5 hashes (dbt `<source>|<id>` keys), so the policy args are VARCHAR.
-- =====================================================================================================

USE ROLE SYSADMIN;
USE WAREHOUSE TRANSFORMING;

CREATE TABLE IF NOT EXISTS ANALYTICS.SECURITY.USER_DEALER_ACCESS (
    user_name    VARCHAR       NOT NULL COMMENT 'Snowflake login name (CURRENT_USER()).',
    tenant_key   VARCHAR       NOT NULL COMMENT 'dim_tenant.tenant_key the user may read.',
    dealer_key   VARCHAR                COMMENT 'dim_dealer.dealer_key; NULL = every dealer in the tenant.',
    granted_by   VARCHAR       NOT NULL,
    granted_at   TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()::TIMESTAMP_NTZ,
    expires_at   TIMESTAMP_NTZ          COMMENT 'Access ends here; NULL = no end date.'
)
COMMENT = 'Row-level access map. Same shape as the semantic model RLS table (user -> tenant, dealer).';

-- Tenant + dealer filter, for dealer-grain tables (dim_dealer, dim_contract, dealer marts, facts).
CREATE ROW ACCESS POLICY IF NOT EXISTS ANALYTICS.SECURITY.RAP_TENANT_DEALER
    AS (row_tenant_key VARCHAR, row_dealer_key VARCHAR) RETURNS BOOLEAN ->
        IS_ROLE_IN_SESSION('TRANSFORMER')
        OR IS_ROLE_IN_SESSION('SYSADMIN')
        OR EXISTS (
            SELECT 1
            FROM ANALYTICS.SECURITY.USER_DEALER_ACCESS AS a
            WHERE a.user_name = CURRENT_USER()
              AND a.tenant_key = row_tenant_key
              AND (a.dealer_key IS NULL OR a.dealer_key = row_dealer_key)
              AND (a.expires_at IS NULL OR a.expires_at > CURRENT_TIMESTAMP()::TIMESTAMP_NTZ)
        )
    COMMENT = 'A reader sees a row only when the access map grants its tenant and dealer.';

-- Tenant-only filter, for tables with no dealer grain (dim_tenant, tenant-level finance marts).
CREATE ROW ACCESS POLICY IF NOT EXISTS ANALYTICS.SECURITY.RAP_TENANT
    AS (row_tenant_key VARCHAR) RETURNS BOOLEAN ->
        IS_ROLE_IN_SESSION('TRANSFORMER')
        OR IS_ROLE_IN_SESSION('SYSADMIN')
        OR EXISTS (
            SELECT 1
            FROM ANALYTICS.SECURITY.USER_DEALER_ACCESS AS a
            WHERE a.user_name = CURRENT_USER()
              AND a.tenant_key = row_tenant_key
              AND (a.expires_at IS NULL OR a.expires_at > CURRENT_TIMESTAMP()::TIMESTAMP_NTZ)
        )
    COMMENT = 'A reader sees a row only when the access map grants its tenant.';

-- dbt rebuilds tables with CREATE OR REPLACE, which drops an attached policy. So TRANSFORMER attaches
-- the policies again after every build (a dbt post-hook on the snowflake target; see README.md).
USE ROLE SECURITYADMIN;
GRANT APPLY ON ROW ACCESS POLICY ANALYTICS.SECURITY.RAP_TENANT_DEALER TO ROLE TRANSFORMER;
GRANT APPLY ON ROW ACCESS POLICY ANALYTICS.SECURITY.RAP_TENANT        TO ROLE TRANSFORMER;
-- Only SYSADMIN maintains the map; the policy reads it as the policy owner, so REPORTER needs no grant.
GRANT SELECT ON TABLE ANALYTICS.SECURITY.USER_DEALER_ACCESS TO ROLE TRANSFORMER;

-- -----------------------------------------------------------------------------------------------------
-- Attach (what the post-hook runs). Shown here for the core tables named in docs/V3_DESIGN.md 2.4.
-- -----------------------------------------------------------------------------------------------------
USE ROLE TRANSFORMER;

ALTER TABLE ANALYTICS.CORE.DIM_DEALER
    ADD ROW ACCESS POLICY ANALYTICS.SECURITY.RAP_TENANT_DEALER ON (tenant_key, dealer_key);
ALTER TABLE ANALYTICS.CORE.DIM_CONTRACT
    ADD ROW ACCESS POLICY ANALYTICS.SECURITY.RAP_TENANT_DEALER ON (tenant_key, dealer_key);
ALTER TABLE ANALYTICS.CORE.DIM_TENANT
    ADD ROW ACCESS POLICY ANALYTICS.SECURITY.RAP_TENANT ON (tenant_key);

-- Example grant for a dealer user and a white-label administrator user (synthetic keys):
--   INSERT INTO ANALYTICS.SECURITY.USER_DEALER_ACCESS (user_name, tenant_key, dealer_key, granted_by)
--   VALUES ('DEALER_D1001_VIEWER', '<tenant_key>', '<dealer_key>', 'access-request-0001'),
--          ('TENANT_ADMIN_VIEWER', '<tenant_key>', NULL,           'access-request-0002');
--
-- Check what a user would see without logging in as them:
--   EXECUTE USING POLICY_CONTEXT(CURRENT_ROLE => 'REPORTER', CURRENT_USER => 'DEALER_D1001_VIEWER')
--   AS SELECT COUNT(*) FROM ANALYTICS.CORE.DIM_DEALER;
