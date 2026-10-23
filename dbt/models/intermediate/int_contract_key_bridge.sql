{{ config(materialized='table') }}
-- Conformed contract key for both source systems (different keyspaces):
--   contract_key = md5('<source_system>|<source contract id>')
-- Admin Core contracts use contract_id; legacy contracts use the integer legacy_contract_id,
-- which is also what Admin Core claims and amendments carry in legacy_contract_ref.
select
    {{ contract_key("'admin_core'", 'contract_id') }} as contract_key,
    'admin_core' as source_system,
    cast(contract_id as varchar) as source_contract_id,
    contract_id as admin_contract_id,
    cast(null as bigint) as legacy_contract_id,
    contract_number_clean as contract_number,
    is_deleted
from {{ ref('stg_admin_core__contract') }}

union all

select
    {{ contract_key("'legacy_admin'", 'legacy_contract_id') }},
    'legacy_admin',
    cast(legacy_contract_id as varchar),
    null,
    legacy_contract_id,
    contract_no,
    is_deleted
from {{ ref('stg_legacy_admin__contracts') }}
