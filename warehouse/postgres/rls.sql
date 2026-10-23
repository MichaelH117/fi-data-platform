-- =====================================================================================================
-- Postgres row-level security for the server warehouse target (dbt target `postgres`)
-- Written, NOT executed locally (no server on the dev box). docker-compose mounts it as an init
-- script, so it runs once on the first boot of the warehouse container.
--
-- Rule (same as Snowflake 04_row_access_policy.sql and the semantic RLS table):
--   a reader sees a mart row only when security.user_dealer_access grants its tenant and dealer.
--   dealer_key NULL in the map = every dealer in that tenant.
--
-- Two ways a reader is identified:
--   * a named login (an analyst):            current_user
--   * a shared app login (report site, portal, BI gateway) that is a member of role `rls_app`:
--       the app runs  SET app.user_name = '<end user>'  per session, and the policy uses that.
--     Only members of rls_app may impersonate this way, so an ordinary reader cannot set the
--     variable to see another dealer.
-- dbt rebuilds tables by create-and-rename, which drops policies. So the policies are attached by a
-- function, security.apply_row_policies(), that dbt calls in an on-run-end hook (see README).
-- =====================================================================================================

-- Roles (Postgres has no CREATE ROLE IF NOT EXISTS).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'transformer') THEN
        CREATE ROLE transformer NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'reporter') THEN
        CREATE ROLE reporter NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'rls_app') THEN
        CREATE ROLE rls_app NOLOGIN;
    END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS security;

CREATE TABLE IF NOT EXISTS security.user_dealer_access (
    access_id   bigserial   PRIMARY KEY,
    user_name   text        NOT NULL,
    tenant_key  text        NOT NULL,
    dealer_key  text,                                -- NULL = every dealer in the tenant
    granted_by  text        NOT NULL,
    granted_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz                          -- NULL = no end date
);
CREATE UNIQUE INDEX IF NOT EXISTS user_dealer_access_uq
    ON security.user_dealer_access (user_name, tenant_key, coalesce(dealer_key, '*'));

COMMENT ON TABLE security.user_dealer_access IS
    'Row-level access map (user -> tenant, dealer). Same shape as the semantic model RLS table.';

-- Who is reading: the app-supplied end user for rls_app members, otherwise the login itself.
-- session_user (the login), not current_user: inside the SECURITY DEFINER check below, current_user
-- is the function owner, and SET ROLE reporter must not change who the reader is.
CREATE OR REPLACE FUNCTION security.effective_user() RETURNS text
LANGUAGE sql STABLE
AS $$
    SELECT CASE
        WHEN pg_has_role(session_user, 'rls_app', 'MEMBER')
            THEN nullif(current_setting('app.user_name', true), '')
        ELSE session_user::text
    END
$$;

-- SECURITY DEFINER so readers do not need SELECT on the map; search_path pinned against hijacking.
CREATE OR REPLACE FUNCTION security.can_read(p_tenant_key text, p_dealer_key text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = security, pg_temp
AS $$
    SELECT pg_has_role(session_user, 'transformer', 'MEMBER')
        OR EXISTS (
            SELECT 1
            FROM security.user_dealer_access AS a
            WHERE a.user_name = security.effective_user()
              AND a.tenant_key = p_tenant_key
              AND (a.dealer_key IS NULL OR p_dealer_key IS NULL OR a.dealer_key = p_dealer_key)
              AND (a.expires_at IS NULL OR a.expires_at > now())
        )
$$;

REVOKE ALL ON security.user_dealer_access FROM PUBLIC;
REVOKE ALL ON FUNCTION security.can_read(text, text) FROM PUBLIC;
GRANT USAGE ON SCHEMA security TO reporter, transformer;
GRANT EXECUTE ON FUNCTION security.effective_user() TO reporter, transformer;
GRANT EXECUTE ON FUNCTION security.can_read(text, text) TO reporter, transformer;
GRANT SELECT ON security.user_dealer_access TO transformer;

-- Attach a SELECT policy to every mart table that carries tenant_key (and dealer_key when present).
-- Idempotent: safe to call after every dbt run. Returns the number of tables it secured.
CREATE OR REPLACE FUNCTION security.apply_row_policies(
    p_schemas text[] DEFAULT ARRAY['core', 'finance', 'claims', 'dealer', 'regulatory']
) RETURNS integer
LANGUAGE plpgsql
AS $$
DECLARE
    t record;
    n integer := 0;
BEGIN
    FOR t IN
        SELECT c.table_schema, c.table_name,
               bool_or(c.column_name = 'dealer_key') AS has_dealer
        FROM information_schema.columns AS c
        JOIN information_schema.tables AS tb
          ON tb.table_schema = c.table_schema AND tb.table_name = c.table_name
        WHERE c.table_schema = ANY (p_schemas)
          AND tb.table_type = 'BASE TABLE'
          AND c.column_name IN ('tenant_key', 'dealer_key')
        GROUP BY c.table_schema, c.table_name
        HAVING bool_or(c.column_name = 'tenant_key')
    LOOP
        EXECUTE format('ALTER TABLE %I.%I ENABLE ROW LEVEL SECURITY', t.table_schema, t.table_name);
        EXECUTE format('DROP POLICY IF EXISTS rls_tenant_dealer ON %I.%I', t.table_schema, t.table_name);
        EXECUTE format(
            'CREATE POLICY rls_tenant_dealer ON %I.%I FOR SELECT TO reporter USING (security.can_read(tenant_key, %s))',
            t.table_schema, t.table_name, CASE WHEN t.has_dealer THEN 'dealer_key' ELSE 'NULL' END);
        EXECUTE format('GRANT SELECT ON %I.%I TO reporter', t.table_schema, t.table_name);
        n := n + 1;
    END LOOP;
    RETURN n;
END
$$;

GRANT EXECUTE ON FUNCTION security.apply_row_policies(text[]) TO transformer;

-- Example (synthetic keys):
--   INSERT INTO security.user_dealer_access (user_name, tenant_key, dealer_key, granted_by)
--   VALUES ('dealer_d1001_viewer', '<tenant_key>', '<dealer_key>', 'access-request-0001');
--   SELECT security.apply_row_policies();
--   SET ROLE reporter; SET app.user_name = 'dealer_d1001_viewer';   -- as an rls_app member
--   SELECT count(*) FROM core.dim_dealer;                            -- only that dealer's row
