{% snapshot snap_claim_status %}
{{ config(unique_key='claim_key', strategy='timestamp', updated_at='updated_at', hard_deletes='invalidate') }}
-- Daily SCD2 claim status (status + substatus), both systems. Earlier history comes from
-- claim_history via int_status_history_backfill.
select claim_key, source_system, claim_number, status, substatus, updated_at
from {{ ref('int_claims_conformed') }}
{% endsnapshot %}
