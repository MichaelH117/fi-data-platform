"""CLI: ``python -m warehouse.postgres sync|reconcile|status [--warehouse PATH]``.

sync       copy the report/semantic schemas from DuckDB into PG_DATABASE, add keys, RLS and grants, reconcile
reconcile  re-check row counts and numeric totals of the published copy against DuckDB (exit 1 on mismatch)
status     is the server reachable, and how many tables/rows does each published schema hold

Exit codes: 0 ok, 1 failed or does not reconcile, 2 Postgres not available.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from warehouse.postgres.sync import SCHEMAS, WAREHOUSE_PATH, Settings, SyncError, available, reconcile_only, sync

log = logging.getLogger("warehouse.postgres")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m warehouse.postgres", description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=("sync", "reconcile", "status"))
    ap.add_argument("--warehouse", type=Path, default=WAREHOUSE_PATH)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    s = Settings.from_env()
    reason = available(s)
    if reason:
        log.error("Postgres is not available: %s (start it with tools/pg.ps1 start)", reason)
        return 2
    try:
        if args.command == "sync":
            sync(args.warehouse, s)
        elif args.command == "reconcile":
            problems, totals = reconcile_only(args.warehouse, s)
            if problems:
                log.error("Postgres does NOT reconcile with DuckDB:\n  %s", "\n  ".join(problems[:30]))
                return 1
            log.info("reconciled %(tables)d tables, %(rows)d rows, float total %(float_sum).2f", totals)
        else:
            with s.connect() as con:
                rows = con.execute(
                    "select n.nspname, count(*), coalesce(sum(c.reltuples::bigint), 0) from pg_class c "
                    "join pg_namespace n on n.oid = c.relnamespace where c.relkind = 'r' and n.nspname = any(%s) "
                    "group by 1 order by 1", [[*SCHEMAS, "security"]]).fetchall()
            log.info("server %s:%s database %s (BI login %s)", s.host, s.port, s.database, s.reader)
            for sch, n, est in rows:
                log.info("  %-15s %3d tables  ~%d rows", sch, n, est)
    except SyncError as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
