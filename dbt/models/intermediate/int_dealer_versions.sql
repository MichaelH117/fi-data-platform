{{ config(materialized='table') }}
-- Every Admin Core dealer version the raw layer holds, as SCD2 rows (valid_from = updated_at,
-- valid_to = the next version's updated_at). Rebuilt from raw; snapshots.snap_dealer is the
-- cross-check.
with versions as (
    select
        * exclude (_loaded_by, _is_deleted, _run_id, removed_from_source),
        'admin_core' as source_system,
        coalesce(_is_deleted, false) or coalesce(removed_from_source, 0) = 1 as is_deleted
    from {{ source('admin_core', 'dealer') }}
)

{{ scd2_from_versions('versions', 'dealer_id') }}
