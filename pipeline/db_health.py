"""Database health (V6 edit 20): ``audit.db_health``, one row per finding, shown on the Operations page.

Three checks, recorded by ``python -m pipeline.diagnostics`` after the layer row counts (informational:
they never block publishing):

  zero_row_table     a table or view in a serving schema (the marts the reports and models read) that
                     is empty. Read from ``audit.layer_row_count`` for the same run.
  stale_statistics   a Postgres serving table whose planner statistics were never gathered, or that
                     changed by more than ``STALE_MOD_SHARE`` of its rows since the last ANALYZE.
  refresh_login_adhoc  a session open under the BI refresh login (``PG_BI_READER_USER``) from a client
                     that is not a scheduled refresh: the refresh login is for refreshes only, people
                     query with their own login.

The Postgres checks need the local server. When it is down (or psycopg / the password is missing) one
``skipped`` row per check says why, and the DuckDB check still runs.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

import duckdb

log = logging.getLogger("pipeline.db_health")

SERVING_SCHEMAS: tuple[str, ...] = ("core", "finance", "claims", "dealer", "regulatory", "actuarial", "gold",
                                    "gold_actuarial")
STALE_MOD_SHARE = 0.10  # rows changed since the last ANALYZE, as a share of live rows
# application_name prefixes of scheduled refresh clients (Power BI service / gateway / mashup engine) and of
# this repo's own sync; any other client under the refresh login is an ad-hoc query.
REFRESH_CLIENTS: tuple[str, ...] = ("powerbi", "power bi", "mashup", "microsoft", "warehouse.postgres")

DDL = """
create schema if not exists audit;
create table if not exists audit.db_health (
    run_id varchar, check_name varchar, engine varchar, object_name varchar, status varchar,
    measured_value double, detail varchar, checked_at timestamp
);
"""
CHECKS = ("zero_row_table", "stale_statistics", "refresh_login_adhoc")

Row = tuple[str, str, str, str, float | None, str]  # check, engine, object, status, value, detail

STATS_SQL = """
select schemaname || '.' || relname, n_live_tup, n_mod_since_analyze,
       greatest(last_analyze, last_autoanalyze) as analyzed_at
from pg_stat_user_tables where schemaname = any(%s)
"""
SESSIONS_SQL = """
select coalesce(application_name, ''), coalesce(client_addr::text, 'local'), state
from pg_stat_activity where usename = %s and pid <> pg_backend_pid()
"""


def zero_row_tables(con: duckdb.DuckDBPyConnection, run_id: str) -> list[Row]:
    """Empty serving relations, from the layer counts this run already recorded."""
    try:
        rows = con.execute(
            "select schema_name || '.' || relation_name from audit.layer_row_count "
            "where run_id = ? and row_count = 0 and schema_name in (select unnest(?::varchar[])) order by 1",
            [run_id, list(SERVING_SCHEMAS)]).fetchall()
    except duckdb.Error:
        return [("zero_row_table", "duckdb", "", "skipped", None, "no layer row counts for this run")]
    if not rows:
        return [("zero_row_table", "duckdb", "all serving tables", "pass", 0.0, "every serving table has rows")]
    return [("zero_row_table", "duckdb", name, "warn", 0.0, "table has zero rows") for (name,) in rows]


def stale_statistics(stats: Sequence[tuple]) -> list[Row]:
    """stats rows: (table, n_live_tup, n_mod_since_analyze, analyzed_at or None)."""
    out: list[Row] = []
    for name, live, mods, analyzed in stats:
        live, mods = int(live or 0), int(mods or 0)
        if analyzed is None:
            out.append(("stale_statistics", "postgres", name, "warn", None, "statistics never gathered (run ANALYZE)"))
        elif live and mods / live > STALE_MOD_SHARE:
            out.append(("stale_statistics", "postgres", name, "warn", mods / live,
                        f"{mods:,} of {live:,} rows changed since ANALYZE"))
    return out or [("stale_statistics", "postgres", "all serving tables", "pass", 0.0, "statistics current")]


def is_refresh_client(application_name: str) -> bool:
    return application_name.strip().lower().startswith(REFRESH_CLIENTS)


def refresh_login_adhoc(sessions: Sequence[tuple], login: str) -> list[Row]:
    """sessions rows: (application_name, client, state) for the refresh login."""
    bad = [s for s in sessions if not is_refresh_client(s[0])]
    if not bad:
        return [("refresh_login_adhoc", "postgres", login, "pass", 0.0, "only refresh clients use the refresh login")]
    return [("refresh_login_adhoc", "postgres", login, "warn", float(len(bad)),
             f"{app or 'unnamed client'} from {client} ({state})") for app, client, state in bad]


def postgres_rows(settings=None, unavailable: Callable[[], str | None] | None = None) -> list[Row]:
    """The two Postgres checks, or one skipped row each when the server cannot be used."""
    from warehouse.postgres import sync

    s = settings or sync.Settings.from_env()
    reason = (unavailable or (lambda: sync.available(s)))()
    if reason:
        return [(c, "postgres", "", "skipped", None, reason) for c in CHECKS[1:]]
    try:
        with s.connect(autocommit=True, timeout=3) as pg:
            stats = pg.execute(STATS_SQL, [list(SERVING_SCHEMAS)]).fetchall()
            sessions = pg.execute(SESSIONS_SQL, [s.reader]).fetchall()
    except Exception as exc:  # noqa: BLE001 - health is informational; never break the gate
        msg = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return [(c, "postgres", "", "skipped", None, msg) for c in CHECKS[1:]]
    return stale_statistics(stats) + refresh_login_adhoc(sessions, s.reader)


def record(con: duckdb.DuckDBPyConnection, run_id: str, pg_rows: Callable[[], list[Row]] = postgres_rows) -> int:
    """Write this run's findings to audit.db_health; returns the number of rows written."""
    rows = zero_row_tables(con, run_id) + pg_rows()
    now = datetime.now(UTC).replace(tzinfo=None)
    con.execute(DDL)
    con.execute("delete from audit.db_health where run_id = ?", [run_id])
    con.executemany("insert into audit.db_health values (?, ?, ?, ?, ?, ?, ?, ?)",
                    [(run_id, *r, now) for r in rows])
    return len(rows)
