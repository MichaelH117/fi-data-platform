{#- V5 foundation helpers (docs/V3_DESIGN.md 8.4). Each rule lives here once; staging calls it. -#}

{#- Placeholder dates that source systems write instead of NULL ("no real date"). Mapped to NULL once,
    in staging, with an is_sentinel_date flag beside it, so no report filters them by hand.
    Override with --vars '{sentinel_dates: [...]}'. -#}
{% macro sentinel_dates() -%}
    {%- set dates = var('sentinel_dates', ['1900-01-01', '1899-12-30']) -%}
    ({% for d in dates %}date '{{ d }}'{% if not loop.last %}, {% endif %}{% endfor %})
{%- endmacro %}

{% macro is_sentinel_date(col) -%}
    coalesce(cast({{ col }} as date) in {{ sentinel_dates() }}, false)
{%- endmacro %}

{#- The column's value, or NULL when it holds a sentinel. Keeps the column's own type. -#}
{% macro sentinel_to_null(col) -%}
    case when {{ is_sentinel_date(col) }} then null else {{ col }} end
{%- endmacro %}

{#- True when any of the listed columns held a sentinel. -#}
{% macro any_sentinel_date(cols) -%}
    ({% for c in cols %}{{ is_sentinel_date(c) }}{% if not loop.last %} or {% endif %}{% endfor %})
{%- endmacro %}

{#- Dealer-number normal form for the dealer master: trimmed, upper case, a leading letter prefix
    added in front of the base number removed ('V-D1039', 'VD1039' and 'd1039 ' -> 'D1039').
    The base form is one letter followed by digits. -#}
{% macro dealer_number_base(col) -%}
    regexp_replace(upper(trim({{ col }})), '^[A-Z]+[-_ ]?([A-Z][0-9]+)$', '\1')
{%- endmacro %}

{#- Dealer-name normal form: upper case, punctuation dropped, whitespace collapsed. -#}
{% macro dealer_name_norm(col) -%}
    trim(regexp_replace(regexp_replace(upper({{ col }}), '[^A-Z0-9 ]', ' ', 'g'), '\s+', ' ', 'g'))
{%- endmacro %}
