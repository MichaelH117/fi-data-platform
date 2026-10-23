-- Diagnostic: freshness and feed integrity.
--   * the latest _loaded_at of each raw source is within var max_raw_age_hours (48 h) of run time;
--   * every landing file failing its manifest checksum or row count was quarantined (never promoted);
--   * every raw landing file is listed in a manifest.
--   * no landing drop is lost: per file source (network feed, CRM, admin API) the loaded sequence
--     numbers run 1..max with no gap, and every drop folder ingest has seen was loaded (a missing or
--     unreadable manifest stays listed until the drop is repaired). Same rule as
--     ingest.network.drop_problems (audit.ingest_drop, Data Engineer).
with loads as (
    select 'admin_core' as source_system, max(_loaded_at) as last_loaded_at from {{ source('admin_core', 'contract') }}
    union all
    select 'legacy_admin', max(_loaded_at) from {{ source('legacy_admin', 'contracts') }}
    union all
    select 'network_feed', max(_loaded_at) from {{ source('network_feed', '_manifest') }}
),

stale as (
    select
        'stale source: ' || source_system as check_name,
        coalesce(datediff('hour', last_loaded_at, current_localtimestamp()), 999999) as detail
    from loads
    where last_loaded_at is null
       or datediff('hour', last_loaded_at, current_localtimestamp()) > {{ var('max_raw_age_hours') }}
),

bad_files as (
    select 'unquarantined bad file: ' || m.source_file as check_name, 1 as detail
    from {{ ref('stg_network_feed__manifest') }} as m
    where not (m.checksum_ok and m.row_count_ok)
      and not exists (
          select 1 from {{ source('network_feed', '_quarantine') }} as q where q._source_file = m.source_file
      )
),

unlisted as (
    select distinct 'file missing from manifest: ' || f._source_file as check_name, 1 as detail
    from (
        select _source_file from {{ source('network_feed', 'deals') }}
        union all select _source_file from {{ source('network_feed', 'remittance') }}
        union all select _source_file from {{ source('network_feed', 'econtract_events') }}
    ) as f
    where f._source_file not in (select source_file from {{ ref('stg_network_feed__manifest') }})
),

drops as (
    select * from {{ source('audit', 'ingest_drop') }}
),

loaded_drops as (
    select distinct source_system, sequence_no
    from drops
    where status = 'loaded' and sequence_no is not null
),

expected_drops as (
    select source_system, unnest(generate_series(1, max(sequence_no))) as sequence_no
    from loaded_drops
    group by source_system
),

drop_gaps as (
    select 'sequence gap: ' || e.source_system || ' #' || e.sequence_no as check_name, 1 as detail
    from expected_drops as e
    left join loaded_drops as l using (source_system, sequence_no)
    where l.sequence_no is null
),

unloaded_drops as (
    select distinct 'landing drop not loaded: ' || u.source_system || '/' || u.drop_name as check_name, 1 as detail
    from drops as u
    where u.status <> 'loaded'
      and not exists (
          select 1 from drops as l
          where l.status = 'loaded' and l.source_system = u.source_system and l.drop_name = u.drop_name
      )
)

select * from stale
union all
select * from bad_files
union all
select * from unlisted
union all
select * from drop_gaps
union all
select * from unloaded_drops
