"""Clutter check: every schema registered, no clutter in a serving tier (docs/V3_DESIGN.md 8.4).

``python -m pipeline.clutter_check [--warehouse PATH] [--postgres] [--pg-database NAME] [--json]``

Reads ``pipeline/schema_registry.yml`` and inspects the DuckDB warehouse (always) and the Postgres serving
database (``--postgres``, only when the server answers; skipped with a note otherwise). Exit code 1 on
any finding, 0 when clean, 2 when the warehouse is missing.

Findings
  unregistered_schema   a schema that has no row in the registry (no owner, purpose, tier or retention)
  registry_incomplete   a registry row without owner, tier, purpose or retention, or with an unknown tier
  backup_name           a serving-tier object named like a copy: *_bak*, *_backup*, *_old, *_tmp, *_temp,
                        *_new, *_copy, *_v2.. or a trailing date stamp (*_20240131)
  personal_object       a serving-tier object that looks personal or scratch (zz_*, tmp_*, test_*, scratch*,
                        sandbox*), or any object in a sandbox-tier (or unregistered personal-looking) schema
  empty_table           a serving-tier table with 0 rows that is not in ``allow_empty``

Pointed at the legacy database (``--postgres --pg-database vsc_legacy``) it lists the clutter the
schema registry exists to prevent; that run is expected to fail.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = Path(__file__).with_name("schema_registry.yml")
WAREHOUSE_PATH = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))
TIERS = ("raw", "staging", "marts", "compat", "ops", "sandbox")
REQUIRED = ("owner", "tier", "purpose", "retention")

BACKUP_RE = re.compile(
    r"(_bak|_backup|_old$|_tmp$|_temp$|_new$|_copy$|_v[2-9]$|_\d{8}$|_\d{6}$|^bak_|^backup_|^old_|^copy_of_)",
    re.IGNORECASE)
PERSONAL_RE = re.compile(r"^(zz_|tmp_|temp_|test_|scratch|sandbox|my_)", re.IGNORECASE)
# DuckDB / Postgres system schemas: never project objects, never registered.
SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "pg_toast"}

log = logging.getLogger("pipeline.clutter_check")


@dataclass(frozen=True)
class Finding:
    database: str  # duckdb | postgres:<name>
    rule: str
    schema: str
    object: str = ""
    detail: str = ""

    def __str__(self) -> str:
        where = f"{self.schema}.{self.object}" if self.object else self.schema
        return f"[{self.database}] {self.rule}: {where}" + (f" ({self.detail})" if self.detail else "")


@dataclass(frozen=True)
class Relation:
    schema: str
    name: str
    kind: str  # table | view
    rows: int | None  # None for views (not counted)


def load_registry(path: Path = REGISTRY) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    doc.setdefault("serving_tiers", ["marts", "compat"])
    doc.setdefault("allow_empty", [])
    doc.setdefault("duckdb", {})
    doc.setdefault("postgres", {})
    return doc


def registry_problems(reg: dict) -> list[Finding]:
    out = []
    for db in ("duckdb", "postgres"):
        for schema, row in (reg.get(db) or {}).items():
            row = row or {}
            missing = [k for k in REQUIRED if not str(row.get(k, "")).strip()]
            if missing:
                out.append(Finding(db, "registry_incomplete", schema, detail="missing " + ", ".join(missing)))
            elif row["tier"] not in TIERS:
                out.append(Finding(db, "registry_incomplete", schema, detail=f"unknown tier {row['tier']!r}"))
    return out


def check(database: str, schemas: Iterable[str], relations: Iterable[Relation], entries: dict,
          serving_tiers: Sequence[str], allow_empty: Iterable[str]) -> list[Finding]:
    """Pure rule engine over a catalog listing (shared by DuckDB and Postgres)."""
    allowed_empty = {a.lower() for a in allow_empty}
    findings: list[Finding] = []
    schemas = sorted({s for s in schemas if s not in SYSTEM_SCHEMAS})
    for s in schemas:
        if s not in entries:
            findings.append(Finding(database, "unregistered_schema", s,
                                    detail="add owner, tier, purpose and retention to pipeline/schema_registry.yml"))
    for r in sorted(relations, key=lambda r: (r.schema, r.name)):
        if r.schema in SYSTEM_SCHEMAS:
            continue
        tier = (entries.get(r.schema) or {}).get("tier")
        if tier == "sandbox" or (tier is None and PERSONAL_RE.search(r.schema)):
            findings.append(Finding(database, "personal_object", r.schema, r.name, "sandbox / personal schema"))
            continue
        if tier is not None and tier not in serving_tiers:
            continue  # an unregistered schema is checked as if it were served (nobody vouched for it)
        if BACKUP_RE.search(r.name):
            findings.append(Finding(database, "backup_name", r.schema, r.name, r.kind))
        elif PERSONAL_RE.search(r.name):
            findings.append(Finding(database, "personal_object", r.schema, r.name, r.kind))
        if r.kind == "table" and r.rows == 0 and f"{r.schema}.{r.name}".lower() not in allowed_empty:
            findings.append(Finding(database, "empty_table", r.schema, r.name, "0 rows"))
    return findings


# ---------------------------------------------------------------------- catalogs
def duck_catalog(con: duckdb.DuckDBPyConnection) -> tuple[list[str], list[Relation]]:
    schemas = [r[0] for r in con.execute(
        "select schema_name from duckdb_schemas() where database_name = current_database() and not internal"
    ).fetchall()]
    rels = [Relation(s, t, "table", None) for s, t in con.execute(
        "select schema_name, table_name from duckdb_tables() where database_name = current_database() "
        "and not internal and not temporary").fetchall()]
    rels += [Relation(s, v, "view", None) for s, v in con.execute(
        "select schema_name, view_name from duckdb_views() where database_name = current_database() "
        "and not internal and not temporary").fetchall()]
    counted = []
    for r in rels:
        if r.kind == "table":
            n = con.execute(f'select count(*) from "{r.schema}"."{r.name}"').fetchone()[0]
            r = Relation(r.schema, r.name, r.kind, int(n))
        counted.append(r)
    return schemas, counted


def pg_catalog(cur) -> tuple[list[str], list[Relation]]:
    """Schemas and relations from pg_catalog (exact row counts: serving databases are small)."""
    cur.execute("select nspname from pg_namespace where nspname not like 'pg\\_%' and nspname <> 'information_schema'")
    schemas = [r[0] for r in cur.fetchall()]
    cur.execute(
        "select n.nspname, c.relname, case when c.relkind in ('v', 'm') then 'view' else 'table' end "
        "from pg_class c join pg_namespace n on n.oid = c.relnamespace "
        "where c.relkind in ('r', 'p', 'v', 'm') and n.nspname not like 'pg\\_%' "
        "and n.nspname <> 'information_schema' and not c.relispartition")
    rels = []
    for s, t, kind in cur.fetchall():
        rows = None
        if kind == "table":
            cur.execute(f'select count(*) from "{s}"."{t}"')
            rows = int(cur.fetchone()[0])
        rels.append(Relation(s, t, kind, rows))
    return schemas, rels


# ---------------------------------------------------------------------- runners
def check_duckdb(warehouse: Path, reg: dict) -> list[Finding]:
    with duckdb.connect(str(warehouse), read_only=True) as con:
        schemas, rels = duck_catalog(con)
    return check("duckdb", schemas, rels, reg["duckdb"], reg["serving_tiers"], reg["allow_empty"])


def check_postgres(reg: dict, database: str | None = None) -> tuple[list[Finding], str | None]:
    """(findings, skip reason). Skips, never fails, when the server is not available."""
    from warehouse.postgres.sync import Settings, available

    s = Settings.from_env()
    reason = available(s)
    if reason:
        return [], reason
    db = database or s.database
    with s.connect(database=db, autocommit=True) as con, con.cursor() as cur:
        schemas, rels = pg_catalog(cur)
    return check(f"postgres:{db}", schemas, rels, reg["postgres"], reg["serving_tiers"], reg["allow_empty"]), None


def run(warehouse: Path = WAREHOUSE_PATH, postgres: bool = False, pg_database: str | None = None,
        registry: Path = REGISTRY) -> tuple[list[Finding], list[str]]:
    reg = load_registry(registry)
    findings = registry_problems(reg)
    notes: list[str] = []
    findings += check_duckdb(warehouse, reg)
    if postgres:
        pg, reason = check_postgres(reg, pg_database)
        findings += pg
        if reason:
            notes.append(f"postgres skipped: {reason}")
    return findings, notes


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.clutter_check", description=__doc__.splitlines()[0])
    ap.add_argument("--warehouse", type=Path, default=WAREHOUSE_PATH)
    ap.add_argument("--registry", type=Path, default=REGISTRY)
    ap.add_argument("--postgres", action="store_true", help="also check the Postgres serving database when it is up")
    ap.add_argument("--pg-database", help="Postgres database to check (default PG_DATABASE)")
    ap.add_argument("--json", action="store_true", help="print findings as JSON")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    if not args.warehouse.exists():
        log.error("warehouse not found: %s (run python -m pipeline.run first)", args.warehouse)
        return 2
    findings, notes = run(args.warehouse, args.postgres, args.pg_database, args.registry)
    for n in notes:
        log.info(n)
    if args.json:
        print(json.dumps([asdict(f) for f in findings], indent=2))
    for f in findings:
        log.error("%s", f)
    if findings:
        log.error("clutter check FAILED: %d finding(s)", len(findings))
        return 1
    log.info("clutter check passed: every schema registered, no clutter in serving tiers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
