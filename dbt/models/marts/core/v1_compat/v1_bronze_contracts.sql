-- DEPRECATED V1 name (bronze.contracts): every raw contract row version from both systems.
-- Use raw_legacy_admin.contracts / raw_admin_core.contract (or core.dim_contract) instead.
{{ config(schema='bronze', alias='contracts') }}
select
    {{ contract_key("'legacy_admin'", 'legacy_contract_id') }} as contract_id,
    updated_at,
    _loaded_at,
    _source_system
from {{ source('legacy_admin', 'contracts') }}
union all
select
    {{ contract_key("'admin_core'", 'contract_id') }},
    updated_at,
    _loaded_at,
    _source_system
from {{ source('admin_core', 'contract') }}
