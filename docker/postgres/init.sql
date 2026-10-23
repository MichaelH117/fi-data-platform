-- Runs once on first boot of the warehouse container. UNTESTED (no Docker on the dev box).
-- Airflow's metadata database lives beside the warehouse to save a container in dev.
-- Password must match AIRFLOW_DB_PASSWORD in docker-compose.yml / .env.
-- Row-level security runs next, from warehouse/postgres/rls.sql (mounted as 02_rls.sql).
CREATE USER airflow WITH PASSWORD 'airflow';
CREATE DATABASE airflow OWNER airflow;

-- Same schema names as the local DuckDB warehouse (docs/V3_DESIGN.md 2.3).
CREATE SCHEMA IF NOT EXISTS raw_admin_core;
CREATE SCHEMA IF NOT EXISTS raw_legacy_admin;
CREATE SCHEMA IF NOT EXISTS raw_network_feed;
CREATE SCHEMA IF NOT EXISTS audit;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS intermediate;
CREATE SCHEMA IF NOT EXISTS seeds;
CREATE SCHEMA IF NOT EXISTS snapshots;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS finance;
CREATE SCHEMA IF NOT EXISTS claims;
CREATE SCHEMA IF NOT EXISTS dealer;
CREATE SCHEMA IF NOT EXISTS regulatory;
