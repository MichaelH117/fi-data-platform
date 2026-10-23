{{ config(materialized='table') }}
-- Every claim header version the raw layer holds, both systems, on the conformed claim_key and
-- Admin Core's status vocabulary, as SCD2 rows: valid_from = updated_at, valid_to = the next
-- version's updated_at. A converted claim carries its legacy versions first, then its Admin Core
-- versions. Rebuilt from raw; snapshots.snap_claim_status is the cross-check.
with versions as (
    select
        {{ claim_key('claim_number') }} as claim_key,
        'admin_core' as source_system,
        {{ normalize_claim_number('claim_number') }} as claim_number,
        upper(trim(status)) as status,
        upper(trim(substatus)) as substatus,
        cast(updated_at as timestamp) as updated_at,
        coalesce(_is_deleted, false) or coalesce(removed_from_source, 0) = 1 as is_deleted,
        _source_version,
        _loaded_at
    from {{ source('admin_core', 'claim') }}

    union all

    select
        {{ claim_key('claim_no') }},
        'legacy_admin',
        {{ normalize_claim_number('claim_no') }},
        case lower(trim(status)) when 'paid' then 'CLOSED_FULLY_PAID' when 'denied' then 'DENIED' else 'OPEN' end,
        case lower(trim(status)) when 'paid' then 'PAID' when 'denied' then 'NOTIFIED' end,
        cast(updated_at as timestamp),
        coalesce(_is_deleted, false) or coalesce(removed_from_source, 0) = 1,
        _source_version,
        _loaded_at
    from {{ source('legacy_admin', 'claims') }}
)

{{ scd2_from_versions('versions', 'claim_key') }}
