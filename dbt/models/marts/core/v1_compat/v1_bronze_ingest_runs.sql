-- DEPRECATED V1 name (bronze._ingest_runs). Use audit.ingest_run.
-- table_name is qualified with the source system (crm.calls, admin_api.dealer, admin_core.dealer, ...)
-- so the quality page's latest-batch-per-table list shows every intake source, including the CRM and
-- admin-API quarantine counts, instead of collapsing same-named tables from different sources.
{{ config(schema='bronze', alias='_ingest_runs') }}
select run_id as _batch_id, source_system, source_system || '.' || table_name as table_name, watermark_from,
       watermark_to, rows_read, rows_loaded, rows_quarantined, rows_deleted, status, message, started_at, finished_at
from {{ source('audit', 'ingest_run') }}
