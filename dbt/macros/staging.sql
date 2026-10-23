{#- Staging helpers for the append-only raw layer. -#}

{#- Latest version of each key in a raw table (highest _source_version, then latest load).
    Pipeline columns are dropped except _source_system and _loaded_at; the delete marker and
    the source soft-delete flag become one `is_deleted` column. `exclude` drops extra columns
    (PII the model re-derives). -#}
{% macro latest_version(relation, key, exclude=[], soft_delete=true) -%}
    select
        * exclude (_loaded_by, _source_version, _is_deleted, _run_id
                   {%- if soft_delete %}, removed_from_source{% endif %}
                   {%- for c in exclude %}, {{ c }}{% endfor %}),
        (coalesce(_is_deleted, false)
            {%- if soft_delete %} or coalesce(removed_from_source, 0) = 1{% endif %}) as is_deleted
    from {{ relation }}
    qualify row_number() over (
        partition by {{ key }}
        order by _source_version desc nulls last, _loaded_at desc
    ) = 1
{%- endmacro %}

{#- Admin Core table: latest version per primary key. -#}
{% macro stg_admin_core(table, key, exclude=[]) -%}
    {{ latest_version(source('admin_core', table), key, exclude) }}
{%- endmacro %}

{#- Network feed rows minus every row the feed step rejected (whole bad files and bad rows). -#}
{% macro feed_accepted(table) -%}
    select f.*
    from {{ source('network_feed', table) }} as f
    where not exists (
        select 1
        from {{ source('network_feed', '_quarantine') }} as q
        where q._source_file = f._source_file
          and (q._row_number = f._row_number or q._row_number is null or q._row_number = 0)
    )
{%- endmacro %}

{#- CRM / admin-API intake rows minus every row ingest quarantined (contract rejects, failed
    checksums, incomplete extracts). Same anti-join rule as the dealer feed. -#}
{% macro intake_accepted(source_name, table) -%}
    select r.*
    from {{ source(source_name, table) }} as r
    where not coalesce(r._is_deleted, false)
      and not exists (
        select 1
        from {{ source(source_name, '_quarantine') }} as q
        where q._source_file = r._source_file
          and (q._row_number = r._row_number or q._row_number is null or q._row_number = 0)
    )
{%- endmacro %}

{#- Every distinct source version of an entity as SCD2 rows, built from the append-only raw layer
    (not from dbt run times), so history can be rebuilt at any time and does not depend on when
    dbt ran. `versions` is a relation / CTE with `key`, updated_at, _source_version, _loaded_at.
    A version loaded twice (re-ingest) counts once. valid_from = updated_at; valid_to = the next
    version's updated_at; is_current marks the newest version. -#}
{% macro scd2_from_versions(versions, key) -%}
    select
        *,
        updated_at as valid_from,
        lead(updated_at) over w as valid_to,
        lead(updated_at) over w is null as is_current,
        row_number() over w as version_no
    from (
        select *
        from {{ versions }}
        qualify row_number() over (
            partition by {{ key }}, source_system, _source_version order by _loaded_at
        ) = 1
    )
    window w as (partition by {{ key }} order by updated_at, _source_version, _loaded_at)
{%- endmacro %}

{#- Legacy contract status in Admin Core's vocabulary (active -> INFORCE, ...). -#}
{% macro legacy_contract_status(col) -%}
    case lower(trim({{ col }}))
        when 'active' then 'INFORCE'
        when 'cancelled' then 'CANCELLED'
        when 'expired' then 'EXPIRED'
        else upper(trim({{ col }}))
    end
{%- endmacro %}
