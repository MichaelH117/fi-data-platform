-- Admin Core custom fields (EAV) pivoted ONCE into typed columns: one row per contract that has any
-- custom-field value. The source stores every value as text in contract_custom_field (contract_id,
-- field_id, value_text) with the meaning in custom_field_def (field_name, data_type). Analysts used
-- to repeat `max(case when field_id = ... then value end)` in every query; here the pivot is by
-- field NAME (ids differ by tenant), values are cast to their declared type, and a value that does
-- not cast is counted in invalid_value_count instead of silently becoming NULL.
-- The chassis VIN is hashed like every other VIN (PII never leaves staging).
-- Until the source tables exist in raw (older builds), the model is empty with the same columns.
{%- set fields = [
    ('Coach Make', 'coach_make', 'varchar'),
    ('Coach Model', 'coach_model', 'varchar'),
    ('Chassis Make', 'chassis_make', 'varchar'),
    ('Chassis VIN', 'chassis_vin', 'varchar'),
    ('Model Year', 'model_year', 'integer'),
] %}
{%- set has_source = true %}
{%- if execute %}
    {%- set rel = adapter.get_relation(database=source('admin_core', 'contract_custom_field').database,
                                       schema=source('admin_core', 'contract_custom_field').schema,
                                       identifier='contract_custom_field') %}
    {%- set rel_def = adapter.get_relation(database=source('admin_core', 'custom_field_def').database,
                                           schema=source('admin_core', 'custom_field_def').schema,
                                           identifier='custom_field_def') %}
    {%- set has_source = rel is not none and rel_def is not none %}
{%- endif %}

{% if has_source %}
with vals as (
    {{ stg_admin_core('contract_custom_field', 'custom_field_id') }}
),

defs as (
    {{ stg_admin_core('custom_field_def', 'field_def_id') }}
),

typed as (
    select
        v.contract_id,
        v.tenant_id,
        d.field_name,
        lower(d.data_type) as data_type,
        nullif(trim(v.value_text), '') as value_text
    from vals as v
    inner join defs as d
        on d.field_id = v.field_id and d.tenant_id = v.tenant_id and not d.is_deleted
    where not v.is_deleted
)

select
    contract_id,
    any_value(tenant_id) as tenant_id,
    {%- for name, col, typ in fields %}
    {%- if col == 'chassis_vin' %}
    {{ hash_pii("upper(max(case when field_name = '" ~ name ~ "' then value_text end))") }} as chassis_vin_hash,
    {%- elif typ == 'varchar' %}
    max(case when field_name = '{{ name }}' then value_text end) as {{ col }},
    {%- else %}
    max(case when field_name = '{{ name }}' then try_cast(value_text as {{ typ }}) end) as {{ col }},
    {%- endif %}
    {%- endfor %}
    count(*) filter (where value_text is not null and data_type = 'integer'
                     and try_cast(value_text as integer) is null) as invalid_value_count,
    count(*) as custom_field_count
from typed
group by contract_id
{% else %}
select
    cast(null as integer) as contract_id,
    cast(null as integer) as tenant_id,
    {%- for name, col, typ in fields %}
    {%- if col == 'chassis_vin' %}
    cast(null as varchar) as chassis_vin_hash,
    {%- else %}
    cast(null as {{ typ }}) as {{ col }},
    {%- endif %}
    {%- endfor %}
    cast(0 as bigint) as invalid_value_count,
    cast(0 as bigint) as custom_field_count
where false
{% endif %}
