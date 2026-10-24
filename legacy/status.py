"""What is built: databases, per-schema object counts (pg_catalog, never information_schema), last build."""

from __future__ import annotations

import json

from legacy import ADMIN_SCHEMA, SCHEMAS, STATE_DIR, databases

OBJECTS_SQL = """
select n.nspname,
       count(*) filter (where c.relkind = 'r') as tables,
       count(*) filter (where c.relkind = 'v') as views,
       count(*) filter (where c.relkind = 'i') as indexes,
       coalesce(sum(c.reltuples) filter (where c.relkind = 'r' and c.reltuples > 0), 0)::bigint as est_rows
from pg_catalog.pg_class c join pg_catalog.pg_namespace n on n.oid = c.relnamespace
where n.nspname = any(%s) group by 1 order by 1"""
CONSTRAINTS_SQL = """
select count(*) from pg_catalog.pg_constraint k join pg_catalog.pg_namespace n on n.oid = k.connamespace
where n.nspname = any(%s) and k.contype in ('p', 'f', 'u')"""


def status(settings=None) -> dict:
    from warehouse.postgres.sync import Settings

    s = settings or Settings.from_env()
    out: dict = {}
    for db, schemas in zip(databases(), (list(SCHEMAS), [ADMIN_SCHEMA]), strict=True):
        with s.connect(database="postgres", autocommit=True) as pc:
            if not pc.execute("select 1 from pg_database where datname = %s", (db,)).fetchone():
                out[db] = "not built (python -m legacy build)"
                continue
        with s.connect(database=db, autocommit=True) as con:
            rows = con.execute(OBJECTS_SQL, (schemas,)).fetchall()
            out[db] = {"schemas": {r[0]: {"tables": r[1], "views": r[2], "indexes": r[3], "est_rows": r[4]}
                                   for r in rows},
                       "pk_fk_unique_constraints": con.execute(CONSTRAINTS_SQL, (schemas,)).fetchone()[0]}
    manifest = STATE_DIR / f"build_{databases()[0]}.json"
    if manifest.exists():
        m = json.loads(manifest.read_text(encoding="utf-8"))
        out["last_build"] = {k: m.get(k) for k in ("built_at", "as_of", "warehouse", "timings")}
    return out
