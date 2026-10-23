{#- Small, dependency-free helpers (no dbt packages, so `dbt deps` / network is never needed). -#}

{#- Deterministic surrogate key: md5 of the '|'-joined, null-safe column values. -#}
{% macro surrogate_key(columns) -%}
    md5(concat_ws('|', {% for c in columns %}coalesce(cast({{ c }} as varchar), '~null~'){% if not loop.last %}, {% endif %}{% endfor %}))
{%- endmacro %}

{#- Conformed contract key: md5('<source_system>|<source id>'). Same rule for every source. -#}
{% macro contract_key(source_system, id_col) -%}
    md5({{ source_system }} || '|' || cast({{ id_col }} as varchar))
{%- endmacro %}

{#- Conformed claim key: md5('claim|<claim number without leading zeros>'). -#}
{% macro claim_key(number_col) -%}
    md5('claim|' || {{ normalize_claim_number(number_col) }})
{%- endmacro %}

{#- '0000123' and '123' are the same claim. -#}
{% macro normalize_claim_number(col) -%}
    nullif(ltrim(trim(cast({{ col }} as varchar)), '0'), '')
{%- endmacro %}

{#- yyyymmdd integer key for dim_date joins. -#}
{% macro date_key(col) -%}
    cast(strftime(cast({{ col }} as date), '%Y%m%d') as integer)
{%- endmacro %}

{#- yyyymm integer (posting_period_key shape). -#}
{% macro yyyymm(col) -%}
    cast(strftime(cast({{ col }} as date), '%Y%m') as integer)
{%- endmacro %}

{#- First day of a posting period, from its yyyymm key (posting_period_start_date on every fct_).
    make_date runs on DuckDB and Postgres; Snowflake spells it date_from_parts. -#}
{% macro period_start_date(key_col) -%}
    {%- if target.type == 'snowflake' -%}
    date_from_parts(floor(({{ key_col }}) / 100), mod({{ key_col }}, 100), 1)
    {%- else -%}
    make_date(cast(floor(({{ key_col }}) / 100) as integer), cast(({{ key_col }}) % 100 as integer), 1)
    {%- endif -%}
{%- endmacro %}

{#- Safe division: null when the denominator is 0 or null. -#}
{% macro safe_divide(num, den) -%}
    ({{ num }}) / nullif({{ den }}, 0)
{%- endmacro %}

{#- Clean a dealer code: trim, uppercase, blank -> null. -#}
{% macro clean_code(col) -%}
    nullif(upper(trim({{ col }})), '')
{%- endmacro %}

{#- PII is never stored past staging: sha256 of the trimmed, lower-cased value. -#}
{% macro hash_pii(col) -%}
    case when nullif(trim(cast({{ col }} as varchar)), '') is not null
         then sha256(lower(trim(cast({{ col }} as varchar)))) end
{%- endmacro %}

{#- 17 characters, no I/O/Q. -#}
{% macro is_valid_vin(col) -%}
    coalesce(regexp_full_match(upper(trim({{ col }})), '[A-HJ-NPR-Z0-9]{17}'), false)
{%- endmacro %}

{#- Odometer-at-sale band (dim_contract_vehicle, dim_contract_actuarial_class). -#}
{% macro odometer_band(col) -%}
    case
        when {{ col }} is null then 'unknown'
        when {{ col }} < 7500 then '0-7.5k'
        when {{ col }} < 30000 then '7.5k-30k'
        when {{ col }} < 60000 then '30k-60k'
        when {{ col }} < 90000 then '60k-90k'
        else '90k+'
    end
{%- endmacro %}

{#- Term band in months. -#}
{% macro term_band(col) -%}
    case
        when {{ col }} is null then 'unknown'
        when {{ col }} <= 12 then '0-12'
        when {{ col }} <= 36 then '13-36'
        when {{ col }} <= 48 then '37-48'
        when {{ col }} <= 60 then '49-60'
        else '61+'
    end
{%- endmacro %}

{#- Mart audit column. -#}
{% macro audit_columns() -%}
    current_timestamp as _built_at
{%- endmacro %}
