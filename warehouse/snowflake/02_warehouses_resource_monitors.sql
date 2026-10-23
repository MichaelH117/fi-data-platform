-- =====================================================================================================
-- 02  Virtual warehouses and resource monitors (cost controls)
-- Written for Snowflake, NOT executed by this repo.
--
-- One warehouse per workload, so cost is attributable and one workload cannot starve another:
--   LOADING       ingest / COPY INTO / CDC merges         LOADER role
--   TRANSFORMING  dbt build                               TRANSFORMER role
--   REPORTING     Power BI, report site, dealer packets   REPORTER role
-- All start at X-Small (1 credit/hour; each size up doubles it), start suspended and resume on demand.
-- ETL warehouses suspend after 60 s idle. The BI warehouse waits 5 minutes, so a dashboard user who
-- clicks around does not pay a cold start on every visual.
-- =====================================================================================================

USE ROLE SYSADMIN;

CREATE WAREHOUSE IF NOT EXISTS LOADING WITH
    WAREHOUSE_SIZE = 'XSMALL'
    AUTO_SUSPEND = 60
    AUTO_RESUME = TRUE
    INITIALLY_SUSPENDED = TRUE
    STATEMENT_TIMEOUT_IN_SECONDS = 1800
    COMMENT = 'Raw loads: network feed COPY INTO, admin-system CDC.';

CREATE WAREHOUSE IF NOT EXISTS TRANSFORMING WITH
    WAREHOUSE_SIZE = 'XSMALL'
    AUTO_SUSPEND = 60
    AUTO_RESUME = TRUE
    INITIALLY_SUSPENDED = TRUE
    STATEMENT_TIMEOUT_IN_SECONDS = 3600
    COMMENT = 'dbt build (models, snapshots, seeds, tests).';

CREATE WAREHOUSE IF NOT EXISTS REPORTING WITH
    WAREHOUSE_SIZE = 'XSMALL'
    AUTO_SUSPEND = 300
    AUTO_RESUME = TRUE
    INITIALLY_SUSPENDED = TRUE
    STATEMENT_TIMEOUT_IN_SECONDS = 600
    COMMENT = 'BI and report reads. Marts only.';

-- -----------------------------------------------------------------------------------------------------
-- Resource monitors: only ACCOUNTADMIN can create them.
-- Each warehouse gets its own monthly credit quota: notify at 80%, suspend at 100% (running queries
-- finish), hard stop at 110% (running queries are cancelled). The quotas are a starting point for the
-- synthetic volume (about 60k contracts); revisit them after a month of ACCOUNT_USAGE data.
-- To e-mail someone, add NOTIFY_USERS = (<user>, ...) once real users exist and have verified e-mail.
-- -----------------------------------------------------------------------------------------------------
USE ROLE ACCOUNTADMIN;

CREATE RESOURCE MONITOR IF NOT EXISTS RM_LOADING WITH
    CREDIT_QUOTA = 10
    FREQUENCY = MONTHLY
    START_TIMESTAMP = IMMEDIATELY
    TRIGGERS
        ON 80 PERCENT DO NOTIFY
        ON 100 PERCENT DO SUSPEND
        ON 110 PERCENT DO SUSPEND_IMMEDIATE;

CREATE RESOURCE MONITOR IF NOT EXISTS RM_TRANSFORMING WITH
    CREDIT_QUOTA = 20
    FREQUENCY = MONTHLY
    START_TIMESTAMP = IMMEDIATELY
    TRIGGERS
        ON 80 PERCENT DO NOTIFY
        ON 100 PERCENT DO SUSPEND
        ON 110 PERCENT DO SUSPEND_IMMEDIATE;

CREATE RESOURCE MONITOR IF NOT EXISTS RM_REPORTING WITH
    CREDIT_QUOTA = 15
    FREQUENCY = MONTHLY
    START_TIMESTAMP = IMMEDIATELY
    TRIGGERS
        ON 80 PERCENT DO NOTIFY
        ON 100 PERCENT DO SUSPEND
        ON 110 PERCENT DO SUSPEND_IMMEDIATE;

ALTER WAREHOUSE LOADING      SET RESOURCE_MONITOR = RM_LOADING;
ALTER WAREHOUSE TRANSFORMING SET RESOURCE_MONITOR = RM_TRANSFORMING;
ALTER WAREHOUSE REPORTING    SET RESOURCE_MONITOR = RM_REPORTING;

-- Account-wide ceiling: catches any warehouse created later without its own monitor.
CREATE RESOURCE MONITOR IF NOT EXISTS RM_ACCOUNT WITH
    CREDIT_QUOTA = 50
    FREQUENCY = MONTHLY
    START_TIMESTAMP = IMMEDIATELY
    TRIGGERS
        ON 80 PERCENT DO NOTIFY
        ON 100 PERCENT DO SUSPEND;

ALTER ACCOUNT SET RESOURCE_MONITOR = RM_ACCOUNT;

-- Cost attribution: dbt sets QUERY_TAG (profiles.yml `query_tag`), so spend per workload can be read with
--   SELECT query_tag, warehouse_name, SUM(credits_attributed_compute) AS credits
--   FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_ATTRIBUTION_HISTORY
--   WHERE start_time >= DATEADD('day', -30, CURRENT_TIMESTAMP())
--   GROUP BY 1, 2 ORDER BY 3 DESC;
