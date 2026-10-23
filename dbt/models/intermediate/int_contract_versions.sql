{{ config(materialized='table') }}
-- Every contract version the raw layer holds (Admin Core change-tracking versions, legacy
-- updated_at versions incl. rows replayed from quarantine), as SCD2 rows ordered by source
-- version: valid_from = updated_at, valid_to = the next version's updated_at. Rebuilt from raw on
-- every run, so history survives a snapshot reset; snapshots.snap_contract_status is the
-- cross-check (test core_snapshots_agree_with_versions).
with versions as (
    select
        {{ contract_key("'admin_core'", 'contract_id') }} as contract_key,
        'admin_core' as source_system,
        upper(trim(contract_number)) as contract_number,
        upper(trim(status)) as status,
        cast(updated_at as timestamp) as updated_at,
        coalesce(_is_deleted, false) or coalesce(removed_from_source, 0) = 1 as is_deleted,
        _source_version,
        _loaded_at
    from {{ source('admin_core', 'contract') }}

    union all

    select
        {{ contract_key("'legacy_admin'", 'legacy_contract_id') }},
        'legacy_admin',
        contract_no,
        {{ legacy_contract_status('status') }},
        cast(updated_at as timestamp),
        coalesce(_is_deleted, false) or coalesce(removed_from_source, 0) = 1,
        _source_version,
        _loaded_at
    from {{ source('legacy_admin', 'contracts') }}

    union all

    select
        {{ contract_key("'legacy_admin'", "cast(raw_json::json ->> 'legacy_contract_id' as bigint)") }},
        'legacy_admin',
        raw_json::json ->> 'contract_no',
        {{ legacy_contract_status("raw_json::json ->> 'status'") }},
        try_cast(raw_json::json ->> 'updated_at' as timestamp),
        coalesce(_is_deleted, false) or coalesce(try_cast(raw_json::json ->> 'removed_from_source' as bigint), 0) = 1,
        _source_version,
        _loaded_at
    from {{ source('legacy_admin', '_quarantine') }}
    where table_name = 'contracts' and reject_reason = 'bad_date:sale_date'
)

{{ scd2_from_versions('versions', 'contract_key') }}
