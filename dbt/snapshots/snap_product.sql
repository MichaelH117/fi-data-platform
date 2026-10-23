{% snapshot snap_product %}
{{ config(unique_key='product_id', strategy='timestamp', updated_at='updated_at', hard_deletes='invalidate') }}
-- Daily SCD2 history of the Admin Core product catalog (one product per plan year).
select * exclude (_source_system, _loaded_at, is_deleted)
from {{ ref('stg_admin_core__product') }}
where not is_deleted
{% endsnapshot %}
