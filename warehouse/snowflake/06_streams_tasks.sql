-- =====================================================================================================
-- 06  Optional: streams + tasks for near-real-time feed monitoring
-- Written for Snowflake, NOT executed by this repo.
--
-- Airflow stays the orchestrator for the daily build (pipeline/airflow/). Streams and tasks cover the
-- small job that should not wait for the nightly run: record each feed file the moment it lands, so a
-- missing or late dealer-network file is visible before 7 AM instead of after the reports fail.
-- Requires: GRANT EXECUTE TASK ON ACCOUNT TO ROLE LOADER (ACCOUNTADMIN, see 03).
-- =====================================================================================================

USE ROLE LOADER;
USE WAREHOUSE LOADING;

-- Append-only streams: COPY only inserts, so there is no update/delete history to track.
CREATE STREAM IF NOT EXISTS RAW.NETWORK_FEED.DEALS_STREAM
    ON TABLE RAW.NETWORK_FEED.DEALS APPEND_ONLY = TRUE
    COMMENT = 'New deal rows since the arrival task last ran.';

CREATE STREAM IF NOT EXISTS RAW.NETWORK_FEED.REMITTANCE_STREAM
    ON TABLE RAW.NETWORK_FEED.REMITTANCE APPEND_ONLY = TRUE
    COMMENT = 'New remittance rows since the arrival task last ran.';

CREATE TABLE IF NOT EXISTS RAW.AUDIT.FEED_ARRIVAL (
    feed_date        DATE,
    file_name        VARCHAR,
    rows_loaded      NUMBER,
    first_loaded_at  TIMESTAMP_LTZ,
    recorded_at      TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'One row per feed file per load, written by TSK_FEED_ARRIVAL.';

-- Runs every 15 minutes but only resumes the warehouse when a stream has data, so idle polls cost
-- nothing. Reading both streams in one transaction advances both offsets together.
CREATE TASK IF NOT EXISTS RAW.AUDIT.TSK_FEED_ARRIVAL
    WAREHOUSE = LOADING
    SCHEDULE = '15 MINUTE'
    COMMENT = 'Records feed file arrivals from the raw streams.'
    WHEN SYSTEM$STREAM_HAS_DATA('RAW.NETWORK_FEED.DEALS_STREAM')
      OR SYSTEM$STREAM_HAS_DATA('RAW.NETWORK_FEED.REMITTANCE_STREAM')
AS
    INSERT INTO RAW.AUDIT.FEED_ARRIVAL (feed_date, file_name, rows_loaded, first_loaded_at)
    SELECT TO_DATE(SPLIT_PART(_source_file, '/', 1)), _source_file, COUNT(*), MIN(_loaded_at)
    FROM RAW.NETWORK_FEED.DEALS_STREAM
    GROUP BY 1, 2
    UNION ALL
    SELECT TO_DATE(SPLIT_PART(_source_file, '/', 1)), _source_file, COUNT(*), MIN(_loaded_at)
    FROM RAW.NETWORK_FEED.REMITTANCE_STREAM
    GROUP BY 1, 2;

-- Child task: flag a late feed. Runs after the arrival task in the same task graph.
CREATE TABLE IF NOT EXISTS RAW.AUDIT.FEED_LATE_ALERT (
    expected_feed_date DATE,
    checked_at         TIMESTAMP_LTZ DEFAULT CURRENT_TIMESTAMP()
);

CREATE TASK IF NOT EXISTS RAW.AUDIT.TSK_FEED_LATE_CHECK
    WAREHOUSE = LOADING
    COMMENT = 'Writes an alert row when yesterday''s deals file has not arrived by 06:00.'
    AFTER RAW.AUDIT.TSK_FEED_ARRIVAL
AS
    INSERT INTO RAW.AUDIT.FEED_LATE_ALERT (expected_feed_date)
    SELECT DATEADD('day', -1, CURRENT_DATE())
    WHERE HOUR(CURRENT_TIMESTAMP()) >= 6
      AND NOT EXISTS (
          SELECT 1 FROM RAW.AUDIT.FEED_ARRIVAL
          WHERE feed_date = DATEADD('day', -1, CURRENT_DATE()) AND file_name LIKE '%deals.csv'
      );

-- Tasks are created suspended. Resume children before the root.
ALTER TASK RAW.AUDIT.TSK_FEED_LATE_CHECK RESUME;
ALTER TASK RAW.AUDIT.TSK_FEED_ARRIVAL   RESUME;

-- Serverless alternative (no warehouse to size): drop the WAREHOUSE line and add
--   USER_TASK_MANAGED_INITIAL_WAREHOUSE_SIZE = 'XSMALL'
-- Pause everything during a backfill or incident:
--   ALTER TASK RAW.AUDIT.TSK_FEED_ARRIVAL SUSPEND;
