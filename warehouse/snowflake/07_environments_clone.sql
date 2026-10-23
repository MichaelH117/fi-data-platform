-- =====================================================================================================
-- 07  Environments (dev / UAT / prod), zero-copy clones and Time Travel
-- Written for Snowflake, NOT executed by this repo.
--
--   prod   ANALYTICS       built by the scheduled Airflow run (dbt target snowflake)
--   UAT    ANALYTICS_UAT   zero-copy clone of prod; a release candidate is built on top and compared
--   dev    ANALYTICS_DEV   transient, per-developer (01); dbt with SNOWFLAKE_DATABASE=ANALYTICS_DEV
-- A clone copies metadata only, so it is instant and costs nothing until data in it changes.
-- Local equivalent (runs today): python -m pipeline.run --env dev|uat|prod (pipeline/envs.py), then
-- python tools/env_diff.py compares UAT with prod row counts and money totals per mart (the promotion check).
-- =====================================================================================================

USE ROLE SYSADMIN;

-- UAT refresh: clone prod, then hand it to the transformer for the release-candidate build.
CREATE OR REPLACE DATABASE ANALYTICS_UAT CLONE ANALYTICS
    COMMENT = 'UAT: zero-copy clone of ANALYTICS, refreshed before each release.';

USE ROLE SECURITYADMIN;
-- A database clone keeps grants on the objects inside it, but not grants on the database itself.
GRANT USAGE, CREATE SCHEMA ON DATABASE ANALYTICS_UAT TO ROLE TRANSFORMER;
GRANT USAGE ON DATABASE ANALYTICS_UAT TO ROLE REPORTER;

-- Release check (run as TRANSFORMER after the UAT build): row and dollar parity with prod per mart,
-- the same counts-and-sums comparison pipeline/reconcile.py runs locally.
--   SELECT 'fct_transaction' AS model,
--          (SELECT COUNT(*)    FROM ANALYTICS.CORE.FCT_TRANSACTION)     AS prod_rows,
--          (SELECT COUNT(*)    FROM ANALYTICS_UAT.CORE.FCT_TRANSACTION) AS uat_rows,
--          (SELECT SUM(amount) FROM ANALYTICS.CORE.FCT_TRANSACTION)     AS prod_amount,
--          (SELECT SUM(amount) FROM ANALYTICS_UAT.CORE.FCT_TRANSACTION) AS uat_amount;

-- -----------------------------------------------------------------------------------------------------
-- Migration rehearsal: freeze RAW at the cutover timestamp, so the parallel run (old reports vs new
-- marts) compares both systems against exactly the same source rows.
-- -----------------------------------------------------------------------------------------------------
USE ROLE SYSADMIN;
CREATE DATABASE IF NOT EXISTS RAW_CUTOVER_REHEARSAL CLONE RAW
    AT (OFFSET => -3600)
    COMMENT = 'RAW as of one hour ago; source for the cutover parallel-run reconciliation.';

-- -----------------------------------------------------------------------------------------------------
-- Time Travel recipes for incidents (commented: each needs a real timestamp or a dropped object).
-- -----------------------------------------------------------------------------------------------------
-- Look at a mart as it was before a bad load:
--   SELECT * FROM ANALYTICS.CORE.FCT_TRANSACTION AT (TIMESTAMP => '2026-10-21 06:00:00'::TIMESTAMP_LTZ);
-- Restore it by cloning the old version next to it, checking it, then swapping:
--   CREATE TABLE ANALYTICS.CORE.FCT_TRANSACTION_RESTORE CLONE ANALYTICS.CORE.FCT_TRANSACTION
--       BEFORE (STATEMENT => '<query id of the bad load>');
--   ALTER TABLE ANALYTICS.CORE.FCT_TRANSACTION SWAP WITH ANALYTICS.CORE.FCT_TRANSACTION_RESTORE;
-- Bring back a table dropped by mistake:
--   UNDROP TABLE ANALYTICS.CORE.DIM_DEALER;
