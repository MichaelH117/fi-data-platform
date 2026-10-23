-- =====================================================================================================
-- 01  Databases and schemas
-- Written for Snowflake, NOT executed by this repo (no paid account). Run order: 01 -> 07.
-- Run as SYSADMIN. Every statement is idempotent (IF NOT EXISTS), so the file can be re-run.
--
-- Layout mirrors the local DuckDB warehouse one to one (docs/V3_DESIGN.md section 2.3):
--   DuckDB schema            Snowflake
--   raw_admin_core       ->  RAW.ADMIN_CORE
--   raw_legacy_admin     ->  RAW.LEGACY_ADMIN
--   raw_network_feed     ->  RAW.NETWORK_FEED
--   audit                ->  RAW.AUDIT
--   staging, intermediate, seeds, snapshots, core, finance, claims, dealer, regulatory
--                        ->  ANALYTICS.<SAME NAME>
-- =====================================================================================================

USE ROLE SYSADMIN;

-- RAW: append-only landing zone. Only LOADER writes here; REPORTER never reads it.
-- Managed access: grants are controlled centrally (SECURITYADMIN), not by whoever created a table.
-- 7 days of Time Travel needs Enterprise edition; Standard edition caps it at 1.
CREATE DATABASE IF NOT EXISTS RAW
    DATA_RETENTION_TIME_IN_DAYS = 7
    COMMENT = 'Raw landing zone: CDC replicas of the admin systems plus dealer-network feed files. Append-only.';

CREATE SCHEMA IF NOT EXISTS RAW.ADMIN_CORE   WITH MANAGED ACCESS
    COMMENT = 'Admin Core OLTP replica (change-tracking CDC, soft deletes as _is_deleted).';
CREATE SCHEMA IF NOT EXISTS RAW.LEGACY_ADMIN WITH MANAGED ACCESS
    COMMENT = 'Legacy admin system replica (updated_at watermark, separate keyspace).';
CREATE SCHEMA IF NOT EXISTS RAW.NETWORK_FEED WITH MANAGED ACCESS
    COMMENT = 'Dealer-integration network daily files, loaded untouched (see 05_load_network_feed.sql).';
CREATE SCHEMA IF NOT EXISTS RAW.AUDIT        WITH MANAGED ACCESS
    COMMENT = 'Pipeline audit: ingest watermarks and run logs, reconciliation, diagnostics results.';

-- ANALYTICS: everything dbt builds. TRANSFORMER owns the objects it creates.
-- Not managed access, so dbt `grants:` configs and future grants work as usual.
CREATE DATABASE IF NOT EXISTS ANALYTICS
    DATA_RETENTION_TIME_IN_DAYS = 7
    COMMENT = 'dbt-built warehouse: staging -> intermediate -> marts, plus seeds and snapshots.';

CREATE SCHEMA IF NOT EXISTS ANALYTICS.STAGING      COMMENT = 'stg_<source>__<entity> views: rename, cast, dedupe, hash PII.';
CREATE SCHEMA IF NOT EXISTS ANALYTICS.INTERMEDIATE COMMENT = 'int_ models: key bridges, conforming, earning, posting periods.';
CREATE SCHEMA IF NOT EXISTS ANALYTICS.SEEDS        COMMENT = 'Report definitions, status sets, state reserve methods, exclusions.';
CREATE SCHEMA IF NOT EXISTS ANALYTICS.SNAPSHOTS    COMMENT = 'Daily SCD2 snapshots (dbt_valid_from / dbt_valid_to).';
CREATE SCHEMA IF NOT EXISTS ANALYTICS.CORE         COMMENT = 'Shared star schema: dim_* and fct_* keyed by posting period.';
CREATE SCHEMA IF NOT EXISTS ANALYTICS.FINANCE      COMMENT = 'Finance marts: P&L, profit share, commissions, cession experience.';
CREATE SCHEMA IF NOT EXISTS ANALYTICS.CLAIMS       COMMENT = 'Claims marts derived from fct_claim_payment_line.';
CREATE SCHEMA IF NOT EXISTS ANALYTICS.DEALER       COMMENT = 'Dealer marts: production, pending contracts, remittance aging, scorecard.';
CREATE SCHEMA IF NOT EXISTS ANALYTICS.REGULATORY   COMMENT = 'State funded-reserve filings and market surveys.';

-- Row access mapping and policies live apart from the data they protect (see 04_row_access_policy.sql).
CREATE SCHEMA IF NOT EXISTS ANALYTICS.SECURITY WITH MANAGED ACCESS
    COMMENT = 'User -> tenant, dealer access map and row access policies.';

-- Developer sandbox. Transient: no Fail-safe and no Time Travel cost; it is rebuilt from code anyway.
CREATE TRANSIENT DATABASE IF NOT EXISTS ANALYTICS_DEV
    DATA_RETENTION_TIME_IN_DAYS = 0
    COMMENT = 'Per-developer dbt builds (dbt target snowflake with SNOWFLAKE_DATABASE=ANALYTICS_DEV).';
