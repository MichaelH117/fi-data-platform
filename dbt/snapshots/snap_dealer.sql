{% snapshot snap_dealer %}
{{ config(unique_key='dealer_id', strategy='timestamp', updated_at='updated_at', hard_deletes='invalidate') }}
-- Daily SCD2 history of Admin Core dealers (status, group, agent, remit rule, program...).
-- Soft-deleted dealers drop out of the select, so hard_deletes=invalidate closes their row.
select * exclude (_source_system, _loaded_at, is_deleted)
from {{ ref('stg_admin_core__dealer') }}
where not is_deleted
{% endsnapshot %}
