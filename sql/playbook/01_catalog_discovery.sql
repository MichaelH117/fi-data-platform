-- pattern:  catalog / column discovery
-- question: What can I read in this warehouse, how big is each object, which columns does a view really
--           have, and where does a column like "vin" live?
-- trap:     information_schema hides objects you cannot read and says nothing about size or indexes.
--           pg_catalog answers instantly, has_table_privilege tells you what you may select BEFORE a
--           query fails, and reltuples gives an estimated row count for free (no count(*) scan).

set statement_timeout = '30s';

select a.attnum,
       a.attname,
       format_type(a.atttypid, a.atttypmod)
from pg_catalog.pg_attribute a
join pg_catalog.pg_class c on c.oid = a.attrelid
join pg_catalog.pg_namespace n on n.oid = c.relnamespace
where n.nspname = 'legacy_ops'
    and c.relname = 'vw_dim_contracts'
    and a.attnum > 0
    and not a.attisdropped
order by 1;

select n.nspname,
       c.relname,
       a.attname
from pg_catalog.pg_attribute a
join pg_catalog.pg_class c on c.oid = a.attrelid
join pg_catalog.pg_namespace n on n.oid = c.relnamespace
where a.attnum > 0
    and not a.attisdropped
    and c.relkind in ('r', 'v', 'm')
    and a.attname ilike '%vin%'
    and n.nspname not in ('pg_catalog', 'information_schema')
order by 1, 2, 3;

select n.nspname,
       t.relname as table_name,
       i.relname as index_name,
       pg_get_indexdef(x.indexrelid)
from pg_catalog.pg_index x
join pg_catalog.pg_class i on i.oid = x.indexrelid
join pg_catalog.pg_class t on t.oid = x.indrelid
join pg_catalog.pg_namespace n on n.oid = t.relnamespace
where n.nspname in ('legacy_ops', 'legacy_finance', 'legacy_reporting')
order by 1, 2;

select n.nspname,
       c.relname,
       c.relkind,
       c.reltuples::bigint as est_rows,
       has_table_privilege(c.oid, 'select') as i_can_read
from pg_catalog.pg_class c
join pg_catalog.pg_namespace n on n.oid = c.relnamespace
where n.nspname in ('legacy_ops', 'legacy_finance', 'legacy_reporting', 'tenant_b', 'report', 'zz_sandbox_analyst')
    and c.relkind in ('r', 'v', 'm')
order by 1, 2;
