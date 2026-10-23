"""Replicate every source into the raw (bronze) layer of the warehouse, incrementally.

Sources and methods (the run order):
1. Admin Core: change-tracking versions (``row_version``) -> ``raw_admin_core.*`` (ingest/cdc.py)
2. Legacy admin: ``updated_at`` watermark + data contract + key diff -> ``raw_legacy_admin.*``
   (ingest/legacy.py)
3. Landing files: untouched copy per drop + manifest + quarantine -> ``raw_network_feed.*``
   (ingest/network.py)
4. CRM / call-center export and admin-API page drops (``<intake>/crm``, ``<intake>/admin_api``):
   data contract + quarantine -> ``raw_crm.*`` and ``raw_admin_api.*`` (ingest/intake.py)
Watermarks live in ``audit.ingest_watermark`` and every table load is logged to
``audit.ingest_run``. A table only advances its watermark when its rows commit, so a failed table
never loses data and a re-run is safe. One failing table does not stop the others.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import duckdb

from generator import read_source_epoch
from ingest import cdc, intake, legacy, network, raw
from ingest.raw import TableResult

log = logging.getLogger("ingest")
SOURCES = ("admin_core", "legacy_admin", "network_feed", "crm", "admin_api")


def run(
    warehouse: Path,
    admin_core: Path,
    legacy_db: Path,
    landing: Path,
    admin_core_contract: Path,
    legacy_contract: Path,
    sources: tuple[str, ...] = SOURCES,
    full_refresh: bool = False,
    tables: list[str] | None = None,
    intake_dir: Path | None = None,
) -> list[TableResult]:
    warehouse.parent.mkdir(parents=True, exist_ok=True)
    run_ = raw.Run.new()
    results: list[TableResult] = []
    with duckdb.connect(str(warehouse)) as con:
        if full_refresh:
            log.info("full refresh: dropping raw schemas and watermarks for %s", ", ".join(sources))
            for s in sources:
                con.execute(f"DROP SCHEMA IF EXISTS {raw.RAW_SCHEMAS[s]} CASCADE")
            con.execute(raw.AUDIT_DDL)
            con.execute("DELETE FROM audit.ingest_watermark WHERE source_system IN (SELECT unnest(?))", [list(sources)])
            con.execute("DELETE FROM audit.ingest_row_fingerprint WHERE source_system IN (SELECT unnest(?))",
                        [list(sources)])
            con.execute("DELETE FROM audit.ingest_drop WHERE source_system IN (SELECT unnest(?))", [list(sources)])
        raw.setup(con)
        log.info("ingest run %s", run_.run_id)
        epochs = source_epochs(admin_core, legacy_db)
        resets = [] if full_refresh else check_epochs(con, sources, epochs, admin_core, run_)
        results += resets
        if "admin_core" in sources:
            results += cdc.run(con, admin_core, admin_core_contract, run_, tables)
        if "legacy_admin" in sources:
            results += legacy.run(con, legacy_db, legacy_contract, run_, tables)
        if "network_feed" in sources:
            results += network.run(con, landing, run_)
        if {"crm", "admin_api"} & set(sources):
            results += intake.run(con, intake_dir or landing.parent / "intake", run_, tuple(sources))
        reset_sources = {r.source for r in resets}
        for s in sources:
            if epochs.get(s):
                raw.set_epoch(con, s, epochs[s], run_, reset=full_refresh or s in reset_sources)
    for r in results:
        log.info("%-12s %-26s read=%-7d loaded=%-7d quarantined=%-5d deleted=%-5d wm %s -> %s %s", r.source, r.table,
                 r.rows_read, r.rows_loaded, r.rows_quarantined, r.rows_deleted, r.watermark_from, r.watermark_to,
                 "" if r.status == "ok" else f"[{r.status}: {r.message}]")
    return results


def source_epochs(admin_core: Path, legacy_db: Path) -> dict[str, dict | None]:
    """Epoch per source. The landing feed and the intake drops are written by the same build as Admin
    Core, so they share its epoch."""
    core = read_source_epoch(admin_core)
    return {"admin_core": core, "legacy_admin": read_source_epoch(legacy_db), "network_feed": core, "crm": core,
            "admin_api": core}


def _core_version_regressed(con: duckdb.DuckDBPyConnection, admin_core: Path) -> bool:
    """True when the source's change-tracking counter is below a stored watermark (the DB was rebuilt)."""
    top = con.execute("SELECT max(TRY_CAST(watermark_value AS BIGINT)) FROM audit.ingest_watermark "
                      "WHERE source_system = 'admin_core'").fetchone()[0]
    if top is None or not admin_core.exists():
        return False
    src = sqlite3.connect(f"file:{admin_core}?mode=ro", uri=True)
    try:
        version = src.execute("SELECT version FROM _ct_version").fetchone()
    except sqlite3.Error:
        return False
    finally:
        src.close()
    return version is not None and int(version[0]) < int(top)


def check_epochs(con: duckdb.DuckDBPyConnection, sources: tuple[str, ...], epochs: dict, admin_core: Path,
                 run_: raw.Run) -> list[TableResult]:
    """Detect a source that was rebuilt (new seed or a regenerated DB) since the last load and reload its
    raw layer in full, so old and new rows never mix. Downstream, dbt must then run with --full-refresh
    (``audit.source_epoch.reset_run_id`` names this run)."""
    regressed = _core_version_regressed(con, admin_core)
    out = []
    for s in sources:
        cur, stored = epochs.get(s), raw.get_epoch(con, s)
        changed = cur is not None and stored is not None and stored != cur["epoch"]
        has_marks = con.execute("SELECT count(*) FROM audit.ingest_watermark WHERE source_system = ?",
                                [s]).fetchone()[0] > 0
        if not (changed or (regressed and has_marks and s in ("admin_core", "network_feed", "crm", "admin_api"))):
            continue
        why = (f"source epoch changed ({stored} -> {cur['epoch']})" if changed
               else "source change-tracking version is below the stored watermark")
        log.warning("%s: %s; dropping raw and watermarks and reloading in full (run dbt with --full-refresh)",
                    s, why)
        raw.reset_source(con, s)
        res = TableResult(s, "_source_epoch", message=f"source reset: {why}; raw reloaded in full")
        raw.log_run(con, run_, res, datetime.now())
        out.append(res)
    return out


def summary(results: list[TableResult]) -> list[dict]:
    return [asdict(r) for r in results]
