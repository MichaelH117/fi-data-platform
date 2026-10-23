{% snapshot snap_contract_status %}
{{ config(unique_key='contract_key', strategy='timestamp', updated_at='updated_at', hard_deletes='invalidate') }}
-- Daily SCD2 contract status, both systems, from day one. History before the first snapshot
-- comes from int_status_history_backfill.
select contract_key, source_system, contract_number, status, updated_at
from {{ ref('int_contracts_conformed') }}
{% endsnapshot %}
