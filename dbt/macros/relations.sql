{#
  DuckDB names the catalog after the database file, so a view written as
  `warehouse.staging.x` breaks when the file is copied or renamed (a per-agent copy, a test fixture).
  On the duckdb target, ref() and source() therefore render schema.table only and resolve against
  whichever file is open. Server targets (postgres, snowflake) keep fully qualified names, which
  Snowflake needs across the RAW and ANALYTICS databases.
#}
{% macro ref() %}
  {%- set rel = builtins.ref(*varargs, **kwargs) -%}
  {%- if target.type == 'duckdb' and rel is not none and rel.include is defined -%}
    {%- do return(rel.include(database=False)) -%}
  {%- endif -%}
  {%- do return(rel) -%}
{% endmacro %}

{% macro source() %}
  {%- set rel = builtins.source(*varargs, **kwargs) -%}
  {%- if target.type == 'duckdb' and rel is not none and rel.include is defined -%}
    {%- do return(rel.include(database=False)) -%}
  {%- endif -%}
  {%- do return(rel) -%}
{% endmacro %}
