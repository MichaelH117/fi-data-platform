"""Publish the DuckDB marts to a PostgreSQL server (opt-in; the default pipeline never needs it).

``sync`` copies every schema the semantic models and reports read from DuckDB into the Postgres database
(same schema and table names), adds primary keys (from the dbt ``unique`` / ``unique_combination`` tests)
and indexes on ``*_key`` columns, loads the row-level access map into ``security.user_dealer_access``,
applies ``warehouse/postgres/rls.sql``, grants SELECT to the read-only BI login, then reconciles row counts
and numeric (dollar) totals against DuckDB. Everything runs in ONE transaction: a failed copy or a
reconciliation mismatch rolls back, so readers keep the last good copy. Re-running is safe (idempotent).

Connection settings come from the environment, falling back to ``.env`` at the repo root
(see ``.env.example``): PG_HOST, PG_PORT, PG_DATABASE, PG_SUPERUSER, PG_SUPERUSER_PASSWORD,
PG_BI_READER_USER, PG_BI_READER_PASSWORD.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = ROOT / ".env"
RLS_SQL = Path(__file__).with_name("rls.sql")
MARTS_DIR = ROOT / "dbt" / "models" / "marts"
WAREHOUSE_PATH = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))

# Schemas the semantic models (semantic/spec.py) and the report site (reports/) read. gold / gold_actuarial
# are the report-facing views; they land in Postgres as tables. security is built here from
# core.dim_user_dealer_access (the same map the semantic RLS table uses).
SCHEMAS: tuple[str, ...] = ("core", "finance", "dealer", "claims", "regulatory", "actuarial",
                            "gold", "gold_actuarial", "seeds")
RLS_SCHEMAS: tuple[str, ...] = ("core", "finance", "dealer", "claims", "regulatory", "actuarial", "gold",
                                "gold_actuarial")
ACCESS_MAP = "core.dim_user_dealer_access"
GRANTED_BY_MAP = "sync:core.dim_user_dealer_access"
GRANTED_BY_BI = "sync:bi-import-service"  # retired: rows from early syncs, deleted on every sync

log = logging.getLogger("warehouse.postgres")

TYPE_MAP = {
    "BOOLEAN": "boolean", "TINYINT": "smallint", "SMALLINT": "smallint", "INTEGER": "integer",
    "BIGINT": "bigint", "HUGEINT": "numeric(39,0)", "UTINYINT": "smallint", "USMALLINT": "integer",
    "UINTEGER": "bigint", "UBIGINT": "numeric(20,0)", "FLOAT": "real", "DOUBLE": "double precision",
    "DATE": "date", "TIME": "time", "TIMESTAMP": "timestamp", "TIMESTAMP_S": "timestamp",
    "TIMESTAMP_MS": "timestamp", "TIMESTAMP_NS": "timestamp", "TIMESTAMP WITH TIME ZONE": "timestamptz",
    "VARCHAR": "text", "UUID": "uuid", "INTERVAL": "interval", "BLOB": "bytea",
}
EXACT_NUMERIC = ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER",
                 "UBIGINT")
FLOAT_NUMERIC = ("FLOAT", "DOUBLE")


class SyncError(RuntimeError):
    """Raised when the copy cannot be made or does not reconcile."""


# ---------------------------------------------------------------------- settings
def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    """``.env`` values overlaid by the real environment (the environment wins)."""
    vals: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    vals.update({k: v for k, v in os.environ.items() if k.startswith("PG_")})
    return vals


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 5432
    database: str = "vsc_analytics"
    superuser: str = "postgres"
    superuser_password: str = ""
    reader: str = "bi_reader"
    reader_password: str = ""

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = load_env() if env is None else env
        return cls(host=e.get("PG_HOST", "127.0.0.1"), port=int(e.get("PG_PORT", "5432")),
                   database=e.get("PG_DATABASE", "vsc_analytics"), superuser=e.get("PG_SUPERUSER", "postgres"),
                   superuser_password=e.get("PG_SUPERUSER_PASSWORD", ""),
                   reader=e.get("PG_BI_READER_USER", "bi_reader"),
                   reader_password=e.get("PG_BI_READER_PASSWORD", ""))

    def connect(self, *, reader: bool = False, database: str | None = None, autocommit: bool = False,
                timeout: int = 10):
        import psycopg  # opt-in dependency: requirements-postgres.txt

        return psycopg.connect(host=self.host, port=self.port, dbname=database or self.database,
                               user=self.reader if reader else self.superuser,
                               password=self.reader_password if reader else self.superuser_password,
                               connect_timeout=timeout, autocommit=autocommit, application_name="warehouse.postgres")


def available(settings: Settings | None = None) -> str | None:
    """None when the server answers as the superuser, else the reason it cannot be used."""
    try:
        import psycopg  # noqa: F401
    except ImportError:
        return "psycopg is not installed (pip install -r requirements-postgres.txt)"
    s = settings or Settings.from_env()
    if not s.superuser_password:
        return "PG_SUPERUSER_PASSWORD is not set (.env)"
    try:
        with s.connect(database="postgres", autocommit=True, timeout=3) as con:
            con.execute("select 1")
    except Exception as exc:  # noqa: BLE001 - any connect failure means "not available"
        return f"cannot connect to {s.host}:{s.port}: {str(exc).strip().splitlines()[0]}"
    return None


# ---------------------------------------------------------------------- DuckDB catalog
@dataclass
class Table:
    schema: str
    name: str
    columns: list[tuple[str, str]]  # (column, duckdb type)
    primary_key: tuple[str, ...] = ()

    @property
    def fq(self) -> str:
        return f"{self.schema}.{self.name}"

    def pg_type(self, duck_type: str) -> str:
        t = duck_type.upper()
        if t.startswith("DECIMAL"):
            return t.replace("DECIMAL", "numeric").lower()
        if t not in TYPE_MAP:
            raise SyncError(f"{self.fq}: no Postgres type for DuckDB type {duck_type!r}")
        return TYPE_MAP[t]


def _is_exact(t: str) -> bool:
    return t.upper() in EXACT_NUMERIC or t.upper().startswith("DECIMAL")


def _is_float(t: str) -> bool:
    return t.upper() in FLOAT_NUMERIC


def declared_keys(marts_dir: Path = MARTS_DIR) -> dict[str, tuple[str, ...]]:
    """model name -> key columns from the dbt ``unique`` column tests and ``unique_combination`` tests."""
    keys: dict[str, tuple[str, ...]] = {}
    for yml in sorted(marts_dir.rglob("*.yml")):
        doc = yaml.safe_load(yml.read_text(encoding="utf-8")) or {}
        for model in doc.get("models", []) or []:
            name = model.get("name")
            for test in model.get("data_tests", []) or model.get("tests", []) or []:
                if isinstance(test, dict) and "unique_combination" in test:
                    cfg = test["unique_combination"] or {}
                    cols = (cfg.get("arguments") or cfg).get("columns")
                    if cols and name not in keys:
                        keys[name] = tuple(cols)
            for col in model.get("columns", []) or []:
                tests = col.get("data_tests", []) or col.get("tests", []) or []
                if "unique" in tests and name not in keys:
                    keys[name] = (col["name"],)
    return keys


def duck_tables(con, schemas: tuple[str, ...] = SCHEMAS) -> list[Table]:
    rows = con.execute(
        "select table_schema, table_name, column_name, data_type from information_schema.columns "
        f"where table_schema in ({', '.join('?' for _ in schemas)}) order by table_schema, table_name, "
        "ordinal_position", list(schemas)).fetchall()
    tables: dict[tuple[str, str], Table] = {}
    for s, t, c, d in rows:
        tables.setdefault((s, t), Table(s, t, [])).columns.append((c, d))
    keys = declared_keys()
    for tb in tables.values():
        cand = keys.get(tb.name)
        if cand and set(cand) <= {c for c, _ in tb.columns} and _unique(con, tb, cand):
            tb.primary_key = cand
    return [tables[k] for k in sorted(tables)]


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _unique(con, tb: Table, cols: tuple[str, ...]) -> bool:
    cl = ", ".join(_q(c) for c in cols)
    nn = " and ".join(f"{_q(c)} is not null" for c in cols)
    total, distinct, nonnull = con.execute(
        f"select count(*), count(distinct ({cl})), count(*) filter (where {nn}) "
        f"from {_q(tb.schema)}.{_q(tb.name)}").fetchone()
    if not (total == distinct == nonnull):
        log.warning("%s: declared key %s is not unique/non-null in DuckDB; indexed only", tb.fq, cols)
        return False
    return True


# ---------------------------------------------------------------------- reconciliation
def _profile_sql(tb: Table, quote) -> tuple[str, list[str]]:
    cols = [c for c, d in tb.columns if _is_exact(d) or _is_float(d)]
    parts = ["count(*)"] + [f"sum({quote(c)})" for c in cols]
    return f"select {', '.join(parts)} from {quote(tb.schema)}.{quote(tb.name)}", cols


def profile_duck(con, tb: Table) -> tuple[int, dict[str, float | int | None]]:
    q, cols = _profile_sql(tb, _q)
    row = con.execute(q).fetchone()
    return int(row[0]), dict(zip(cols, row[1:], strict=True))


def profile_pg(cur, tb: Table) -> tuple[int, dict[str, float | int | None]]:
    q, cols = _profile_sql(tb, _q)
    cur.execute(q)
    row = cur.fetchone()
    return int(row[0]), dict(zip(cols, row[1:], strict=True))


def _close(a, b, exact: bool) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if exact:
        return int(a) == int(b)
    a, b = float(a), float(b)
    if math.isnan(a) or math.isnan(b):
        return math.isnan(a) and math.isnan(b)
    return abs(a - b) <= max(0.01, 1e-9 * max(abs(a), abs(b)))


def reconcile(duck, pg_cur, tables: list[Table]) -> tuple[list[str], dict[str, float]]:
    """(mismatches, totals): row counts must match exactly, integer sums exactly, float sums to the cent."""
    problems: list[str] = []
    rows_total, float_total = 0, 0.0
    for tb in tables:
        d_n, d_sums = profile_duck(duck, tb)
        p_n, p_sums = profile_pg(pg_cur, tb)
        if d_n != p_n:
            problems.append(f"{tb.fq}: rows duckdb={d_n} postgres={p_n}")
        types = dict(tb.columns)
        for c, dv in d_sums.items():
            if not _close(dv, p_sums.get(c), exact=_is_exact(types[c])):
                problems.append(f"{tb.fq}.{c}: sum duckdb={dv} postgres={p_sums.get(c)}")
            elif _is_float(types[c]) and dv is not None and not math.isnan(float(dv)):
                float_total += float(dv)
        rows_total += d_n
    return problems, {"tables": len(tables), "rows": rows_total, "float_sum": round(float_total, 2)}


# ---------------------------------------------------------------------- sync
def _index_name(table: str, col: str) -> str:
    n = f"ix_{table}__{col}"
    return n if len(n) <= 63 else f"ix_{table[:40]}_{hashlib.sha1(n.encode()).hexdigest()[:12]}"


def _select_list(tb: Table) -> str:
    out = []
    for c, d in tb.columns:
        t = d.upper()
        out.append(f"{_q(c)}::TIMESTAMP as {_q(c)}" if t in ("TIMESTAMP_NS", "TIMESTAMP_S", "TIMESTAMP_MS")
                   else _q(c))
    return ", ".join(out)


def ensure_server_objects(s: Settings) -> None:
    """Database and the read-only login (outside the data transaction: CREATE DATABASE cannot run in one)."""
    from psycopg import sql

    if not s.reader_password:
        raise SyncError("PG_BI_READER_PASSWORD is not set (.env)")
    with s.connect(database="postgres", autocommit=True) as con:
        if not con.execute("select 1 from pg_database where datname = %s", [s.database]).fetchone():
            con.execute(sql.SQL("CREATE DATABASE {} ENCODING 'UTF8' TEMPLATE template0").format(
                sql.Identifier(s.database)))
            log.info("created database %s", s.database)
        exists = con.execute("select 1 from pg_roles where rolname = %s", [s.reader]).fetchone()
        verb = "ALTER" if exists else "CREATE"
        # The Power BI import login reads every row (incl. the unknown-dealer member with no tenant), so it
        # BYPASSes the Postgres row policies; the semantic model's RLS roles scope report viewers. Every other
        # reader (role reporter: analysts, the portal/app logins) stays under rls.sql. Read-only via grants.
        con.execute(sql.SQL(verb + " ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS PASSWORD {}").format(
            sql.Identifier(s.reader), sql.Literal(s.reader_password)))
        con.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(s.database)))
        con.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
            sql.Identifier(s.database), sql.Identifier(s.reader)))
    with s.connect(autocommit=True) as con:
        con.execute(RLS_SQL.read_text(encoding="utf-8"))
        con.execute(sql.SQL("REVOKE reporter FROM {}").format(sql.Identifier(s.reader)))  # direct grants only


def _copy_table(cur, duck, tb: Table, tmp: Path) -> tuple[float, float]:
    """Recreate one table from DuckDB; returns (load seconds, key/index seconds)."""
    from psycopg import sql

    ident = sql.Identifier(tb.schema, tb.name)
    cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(ident))
    cols = sql.SQL(", ").join(sql.SQL("{} {}").format(sql.Identifier(c), sql.SQL(tb.pg_type(d)))
                              for c, d in tb.columns)
    cur.execute(sql.SQL("CREATE TABLE {} ({})").format(ident, cols))
    t_load = time.perf_counter()
    csv = tmp / f"{tb.schema}.{tb.name}.csv"
    duck.execute(f"COPY (select {_select_list(tb)} from {_q(tb.schema)}.{_q(tb.name)}) TO '{csv.as_posix()}' "
                 "(FORMAT csv, HEADER true, NULLSTR '\\N')")
    names = sql.SQL(", ").join(sql.Identifier(c) for c, _ in tb.columns)
    with open(csv, "rb") as fh, cur.copy(sql.SQL(
            "COPY {} ({}) FROM STDIN (FORMAT csv, HEADER true, NULL '\\N', FREEZE true)").format(ident, names)) as cp:
        while chunk := fh.read(1 << 20):
            cp.write(chunk)
    csv.unlink()
    t_idx = time.perf_counter()
    if tb.primary_key:
        cur.execute(sql.SQL("ALTER TABLE {} ADD PRIMARY KEY ({})").format(
            ident, sql.SQL(", ").join(sql.Identifier(c) for c in tb.primary_key)))
    lead = tb.primary_key[:1]
    for c, _ in tb.columns:
        if c.endswith("_key") and (c,) != lead:
            cur.execute(sql.SQL("CREATE INDEX {} ON {} ({})").format(
                sql.Identifier(_index_name(tb.name, c)), ident, sql.Identifier(c)))
    cur.execute(sql.SQL("ANALYZE {}").format(ident))
    return t_idx - t_load, time.perf_counter() - t_idx


def _load_access_map(cur, s: Settings) -> int:
    cur.execute("DELETE FROM security.user_dealer_access WHERE granted_by IN (%s, %s)",
                [GRANTED_BY_MAP, GRANTED_BY_BI])
    cur.execute(
        "INSERT INTO security.user_dealer_access (user_name, tenant_key, dealer_key, granted_by) "
        "SELECT DISTINCT user_login, tenant_key::text, dealer_key, %s FROM core.dim_user_dealer_access "
        "WHERE user_login IS NOT NULL AND tenant_key IS NOT NULL "
        "ON CONFLICT DO NOTHING", [GRANTED_BY_MAP])
    return cur.rowcount


def sync(warehouse: Path = WAREHOUSE_PATH, settings: Settings | None = None,
         schemas: tuple[str, ...] = SCHEMAS) -> dict[str, float]:
    from psycopg import sql

    s = settings or Settings.from_env()
    reason = available(s)
    if reason:
        raise SyncError(f"Postgres is not available: {reason}")
    if not warehouse.exists():
        raise SyncError(f"warehouse not found: {warehouse} (run python -m pipeline.run first)")
    t0 = time.perf_counter()
    ensure_server_objects(s)
    with duckdb.connect(str(warehouse), read_only=True) as duck, tempfile.TemporaryDirectory() as tmpd:
        tables = duck_tables(duck, schemas)
        if not tables:
            raise SyncError(f"no tables in schemas {schemas} in {warehouse}")
        with s.connect() as pg, pg.cursor() as cur:  # one transaction: commit only after reconcile passes
            cur.execute("SET LOCAL client_encoding = 'UTF8'")
            for sch in schemas:
                cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(sch)))
            wanted = {(tb.schema, tb.name) for tb in tables}
            cur.execute("select table_schema, table_name from information_schema.tables "
                        "where table_schema = any(%s) and table_type = 'BASE TABLE'", [list(schemas)])
            for sch, name in cur.fetchall():
                if (sch, name) not in wanted:
                    cur.execute(sql.SQL("DROP TABLE {} CASCADE").format(sql.Identifier(sch, name)))
                    log.info("dropped %s.%s (no longer in DuckDB)", sch, name)
            for i, tb in enumerate(tables, 1):
                load_s, index_s = _copy_table(cur, duck, tb, Path(tmpd))
                log.info("[%d/%d] %s loaded in %.1fs, keys/indexes %.1fs (pk %s)", i, len(tables), tb.fq,
                         load_s, index_s, ", ".join(tb.primary_key) or "-")
            n_map = _load_access_map(cur, s)
            cur.execute("SELECT security.apply_row_policies(%s)", [list(RLS_SCHEMAS)])
            n_rls = cur.fetchone()[0]
            reader = sql.Identifier(s.reader)
            for sch in (*schemas, "security"):
                cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO reporter, {}").format(sql.Identifier(sch), reader))
            for sch in schemas:
                cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO reporter, {}").format(
                    sql.Identifier(sch), reader))
            problems, totals = reconcile(duck, cur, tables)
            if problems:
                pg.rollback()
                raise SyncError("Postgres copy does not reconcile with DuckDB (rolled back):\n  "
                                + "\n  ".join(problems[:30]))
            pg.commit()
    totals.update(rls_tables=n_rls, access_rows=n_map, seconds=round(time.perf_counter() - t0, 1))
    log.info("synced %(tables)d tables, %(rows)d rows, float total %(float_sum).2f; RLS on %(rls_tables)d tables, "
             "%(access_rows)d access-map rows, in %(seconds).1fs", totals)
    return totals


def reconcile_only(warehouse: Path = WAREHOUSE_PATH, settings: Settings | None = None,
                   schemas: tuple[str, ...] = SCHEMAS) -> tuple[list[str], dict[str, float]]:
    """Standalone check of the published copy (also reports tables missing from Postgres)."""
    s = settings or Settings.from_env()
    reason = available(s)
    if reason:
        raise SyncError(f"Postgres is not available: {reason}")
    with duckdb.connect(str(warehouse), read_only=True) as duck, s.connect() as pg, pg.cursor() as cur:
        tables = duck_tables(duck, schemas)
        cur.execute("select table_schema || '.' || table_name from information_schema.tables "
                    "where table_schema = any(%s)", [list(schemas)])
        present = {r[0] for r in cur.fetchall()}
        missing = [tb.fq for tb in tables if tb.fq not in present]
        problems, totals = reconcile(duck, cur, [tb for tb in tables if tb.fq in present])
    return [f"{m}: missing in Postgres" for m in missing] + problems, totals
