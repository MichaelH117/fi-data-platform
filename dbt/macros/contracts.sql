{#- V7: column contracts with the Data Engineer. A raw column the generator adds this round (edits
    21, 23, 24, 45) is read when it exists and falls back to a typed default when it does not, so
    the core models build both before and after the generator change lands. Once a column is in
    every build, the fallback is dead code and can be removed. -#}

{#- True when `relation` has a column named `column` (case-insensitive). False at parse time. -#}
{% macro has_column(relation, column) -%}
    {%- if not execute -%}{{ return(false) }}{%- endif -%}
    {%- set cols = adapter.get_columns_in_relation(relation) | map(attribute='name') | map('lower') | list -%}
    {{ return(column | lower in cols) }}
{%- endmacro %}

{#- `<alias>.<column>` when the column exists, else the default SQL expression. -#}
{% macro column_or(relation, column, default, alias=none) -%}
    {%- if has_column(relation, column) -%}
        {{ (alias ~ '.') if alias else '' }}{{ column }}
    {%- else -%}
        {{ default }}
    {%- endif -%}
{%- endmacro %}
