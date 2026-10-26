-- Diagnostic (V7 edits 27 + 44): a custom-field value that is in the source must still be there in
-- every layer. Counted per layer, raw -> staging -> mart, for the fields the warehouse pivots
-- (coach make and model, chassis make, chassis VIN, model year). The old extract dropped these values
-- while a contract was pending; the warehouse reads the latest source version whatever the status,
-- so any row here means a layer lost a value it was given. Fails on any row.
-- A model-year value that is not a number is not a gap: staging counts it in invalid_value_count.
-- Contracts outside the reportable book (demo dealers, test ranges) are only checked up to staging.
{%- set fields = [
    ('Coach Make', 'coach_make', 'coach_make'),
    ('Coach Model', 'coach_model', 'coach_model'),
    ('Chassis Make', 'chassis_make', 'chassis_make'),
    ('Chassis VIN', 'chassis_vin_hash', 'custom_chassis_vin_hash'),
    ('Model Year', 'model_year', 'custom_model_year'),
] %}
{%- set has_source = true %}
{%- if execute %}
    {%- set src = source('admin_core', 'contract_custom_field') %}
    {%- set has_source = adapter.get_relation(database=src.database, schema=src.schema,
                                              identifier='contract_custom_field') is not none
                         and adapter.get_relation(database=src.database, schema=src.schema,
                                                  identifier='custom_field_def') is not none %}
{%- endif %}

{% if has_source %}
with vals as (
    {{ stg_admin_core('contract_custom_field', 'custom_field_id') }}
),

defs as (
    {{ stg_admin_core('custom_field_def', 'field_def_id') }}
),

in_source as (
    select distinct v.contract_id, d.field_name
    from vals as v
    inner join defs as d
        on d.field_id = v.field_id and d.tenant_id = v.tenant_id and not d.is_deleted
    where not v.is_deleted
      and nullif(trim(v.value_text), '') is not null
      and d.field_name in ({% for name, s, m in fields %}'{{ name }}'{% if not loop.last %}, {% endif %}{% endfor %})
      and not (d.field_name = 'Model Year' and try_cast(trim(v.value_text) as integer) is null)
),

layers as (
    select
        i.contract_id,
        i.field_name,
        {{ contract_key("'admin_core'", 'i.contract_id') }} as contract_key,
        case i.field_name
            {%- for name, s, m in fields %}
            when '{{ name }}' then s.{{ s }} is not null
            {%- endfor %}
        end as in_staging,
        case i.field_name
            {%- for name, s, m in fields %}
            when '{{ name }}' then v.{{ m }} is not null
            {%- endfor %}
        end as in_mart,
        coalesce(r.is_reportable, false) as is_reportable
    from in_source as i
    left join {{ ref('stg_admin_core__contract_custom_field') }} as s on s.contract_id = i.contract_id
    left join {{ ref('int_reportable_scope') }} as r
        on r.contract_key = {{ contract_key("'admin_core'", 'i.contract_id') }}
    left join {{ ref('dim_contract_vehicle') }} as v
        on v.contract_key = {{ contract_key("'admin_core'", 'i.contract_id') }}
)

select contract_key, field_name, 'staging' as missing_in_layer
from layers
where not coalesce(in_staging, false)
union all
select contract_key, field_name, 'mart' as missing_in_layer
from layers
where coalesce(in_staging, false) and is_reportable and not coalesce(in_mart, false)
{% else %}
select cast(null as varchar) as contract_key, cast(null as varchar) as field_name,
    cast(null as varchar) as missing_in_layer
where false
{% endif %}
