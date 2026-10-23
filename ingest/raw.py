"""Shared raw-layer plumbing: audit tables, watermarks, schema-evolving append with audit columns.

Every raw_* table is append-only and carries the audit columns from the V3 convention:
``_loaded_at``, ``_loaded_by`` (``ingest@<run_id>``), ``_source_system``, ``_source_version``,
``_is_deleted`` and ``_run_id``. Staging keeps the latest ``_source_version`` per key.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import duckdb
import pandas as pd

AUDIT_COLS = {
    "_loaded_at": "TIMESTAMP",
    "_loaded_by": "VARCHAR",
    "_source_system": "VARCHAR",
    "_source_version": "BIGINT",
    "_is_deleted": "BOOLEAN",
    "_run_id": "VARCHAR",
}
RAW_SCHEMAS = {"admin_core": "raw_admin_core", "legacy_admin": "raw_legacy_admin", "network_feed": "raw_network_feed",
               "crm": "raw_crm", "admin_api": "raw_admin_api"}

AUDIT_DDL = """
CREATE SCHEMA IF NOT EXISTS audit;
CREATE TABLE IF NOT EXISTS audit.ingest_watermark (
    source_system   VARCHAR NOT NULL,
    table_name      VARCHAR NOT NULL,
    watermark_type  VARCHAR NOT NULL,   -- row_version | updated_at | sequence_no
    watermark_value VARCHAR,
    last_run_id     VARCHAR,
    updated_at      TIMESTAMP,
    PRIMARY KEY (source_system, table_name)
);
CREATE TABLE IF NOT EXISTS audit.source_epoch (
    source_system   VARCHAR PRIMARY KEY,
    epoch           VARCHAR,            -- seed + build id stamped by the generator into the source
    seed            BIGINT,
    last_run_id     VARCHAR,
    reset_run_id    VARCHAR,            -- the ingest run that last found a reset source and reloaded raw
    updated_at      TIMESTAMP
);
CREATE TABLE IF NOT EXISTS audit.ingest_row_fingerprint (
    source_system   VARCHAR NOT NULL,   -- rows already loaded inside a watermark look-back window
    table_name      VARCHAR NOT NULL,
    watermark_value VARCHAR NOT NULL,
    fingerprint     VARCHAR NOT NULL
);
CREATE TABLE IF NOT EXISTS audit.ingest_drop (
    source_system   VARCHAR NOT NULL,   -- network_feed | crm | admin_api: one row per landing drop seen
    drop_name       VARCHAR NOT NULL,   -- landing folder name (feed / export / extract date)
    sequence_no     BIGINT,             -- NULL when the manifest could not be read
    manifest_sha256 VARCHAR,
    status          VARCHAR NOT NULL,   -- loaded | unreadable (an unreadable drop is retried every run)
    run_id          VARCHAR,
    seen_at         TIMESTAMP
);
CREATE TABLE IF NOT EXISTS audit.ingest_run (
    run_id            VARCHAR,
    source_system     VARCHAR,
    table_name        VARCHAR,
    watermark_from    VARCHAR,
    watermark_to      VARCHAR,
    rows_read         BIGINT,
    rows_loaded       BIGINT,
    rows_quarantined  BIGINT,
    rows_deleted      BIGINT,
    status            VARCHAR,
    message           VARCHAR,
    started_at        TIMESTAMP,
    finished_at       TIMESTAMP
);
"""


@dataclass
class TableResult:
    source: str
    table: str
    rows_read: int = 0
    rows_loaded: int = 0
    rows_quarantined: int = 0
    rows_deleted: int = 0
    watermark_from: str | None = None
    watermark_to: str | None = None
    status: str = "ok"
    message: str = ""


@dataclass(frozen=True)
class Run:
    run_id: str
    loaded_at: datetime

    @property
    def loaded_by(self) -> str:
        return f"ingest@{self.run_id}"

    @classmethod
    def new(cls) -> Run:
        run_id = os.environ.get("PIPELINE_RUN_ID") or (
            datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        )
        return cls(run_id, datetime.now().replace(microsecond=0))


def q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def setup(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(AUDIT_DDL)
    for schema in RAW_SCHEMAS.values():
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")


def get_watermark(con: duckdb.DuckDBPyConnection, source: str, table: str) -> str | None:
    row = con.execute(
        "SELECT watermark_value FROM audit.ingest_watermark WHERE source_system = ? AND table_name = ?", [source, table]
    ).fetchone()
    return row[0] if row else None


def set_watermark(con, source: str, table: str, kind: str, value, run: Run) -> None:
    con.execute(
        "INSERT OR REPLACE INTO audit.ingest_watermark VALUES (?, ?, ?, ?, ?, ?)",
        [source, table, kind, None if value is None else str(value), run.run_id, run.loaded_at],
    )


def get_epoch(con, source: str) -> str | None:
    row = con.execute("SELECT epoch FROM audit.source_epoch WHERE source_system = ?", [source]).fetchone()
    return row[0] if row else None


def set_epoch(con, source: str, epoch: dict, run: Run, reset: bool = False) -> None:
    prev = con.execute("SELECT reset_run_id FROM audit.source_epoch WHERE source_system = ?", [source]).fetchone()
    reset_id = run.run_id if reset else (prev[0] if prev else None)
    con.execute("INSERT OR REPLACE INTO audit.source_epoch VALUES (?, ?, ?, ?, ?, ?)",
                [source, epoch["epoch"], epoch.get("seed"), run.run_id, reset_id, run.loaded_at])


def reset_source(con, source: str) -> None:
    """Forget everything loaded from ``source``: its raw schema, watermarks and look-back fingerprints."""
    con.execute(f"DROP SCHEMA IF EXISTS {RAW_SCHEMAS[source]} CASCADE")
    con.execute(f"CREATE SCHEMA IF NOT EXISTS {RAW_SCHEMAS[source]}")
    con.execute("DELETE FROM audit.ingest_watermark WHERE source_system = ?", [source])
    con.execute("DELETE FROM audit.ingest_row_fingerprint WHERE source_system = ?", [source])
    con.execute("DELETE FROM audit.ingest_drop WHERE source_system = ?", [source])


def loaded_drops(con: duckdb.DuckDBPyConnection, source: str, legacy_sql: str | None = None) -> set[int]:
    """Sequence numbers of the drops already loaded from ``source`` (``audit.ingest_drop``).

    A drop is loaded when its sequence number is not in this set, so a late, out-of-order or repaired
    drop still loads after a later one (a high-water mark would skip it forever). ``legacy_sql``
    lists ``(drop_name, sequence_no)`` already in raw; it seeds the set once for a warehouse that was
    loaded before this table existed.
    """
    rows = con.execute("SELECT DISTINCT sequence_no FROM audit.ingest_drop WHERE source_system = ? "
                       "AND status = 'loaded' AND sequence_no IS NOT NULL", [source]).fetchall()
    if not rows and legacy_sql:
        try:
            seeded = con.execute(legacy_sql).fetchall()
        except duckdb.Error:  # raw table not created yet: nothing loaded
            seeded = []
        for name, seq in seeded:
            con.execute("INSERT INTO audit.ingest_drop VALUES (?, ?, ?, NULL, 'loaded', 'backfill', ?)",
                        [source, str(name), int(seq), datetime.now()])
        rows = [(s,) for _, s in seeded]
    return {int(r[0]) for r in rows}


def record_drop(con: duckdb.DuckDBPyConnection, source: str, drop_name: str, seq: int | None, digest: str | None,
                status: str, run: Run) -> None:
    """Log one landing drop. ``unreadable`` keeps one row per drop (latest sighting); ``loaded`` appends."""
    if status != "loaded":
        con.execute("DELETE FROM audit.ingest_drop WHERE source_system = ? AND drop_name = ? AND status = ?",
                    [source, drop_name, status])
    con.execute("INSERT INTO audit.ingest_drop VALUES (?, ?, ?, ?, ?, ?, ?)",
                [source, drop_name, seq, digest, status, run.run_id, run.loaded_at])


def log_run(con, run: Run, res: TableResult, started: datetime) -> None:
    con.execute(
        "INSERT INTO audit.ingest_run VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [run.run_id, res.source, res.table, res.watermark_from, res.watermark_to, res.rows_read, res.rows_loaded,
         res.rows_quarantined, res.rows_deleted, res.status, res.message, started, datetime.now()],
    )


def ensure_table(con: duckdb.DuckDBPyConnection, schema: str, table: str, columns: dict[str, str]) -> None:
    """Create the raw table, or add any new source columns (schema drift is additive, never destructive)."""
    full = {**columns, **AUDIT_COLS}
    exists = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = ? AND table_name = ?", [schema, table]
    ).fetchone()[0]
    if not exists:
        cols = ", ".join(f"{q(c)} {t}" for c, t in full.items())
        con.execute(f"CREATE TABLE {schema}.{q(table)} ({cols})")
        return
    have = {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = ? AND table_name = ?", [schema, table]
    ).fetchall()}
    for c, t in full.items():
        if c not in have:
            con.execute(f"ALTER TABLE {schema}.{q(table)} ADD COLUMN {q(c)} {t}")


def append(con: duckdb.DuckDBPyConnection, schema: str, table: str, df: pd.DataFrame, columns: dict[str, str],
           run: Run, source: str, version: pd.Series | int, deleted: pd.Series | bool) -> int:
    """Append ``df`` (source columns) plus audit columns, casting each column to its declared type."""
    if df.empty:
        return 0
    frame = df[[c for c in columns if c in df.columns]].copy()
    frame = frame.astype(object).where(frame.notna(), None)
    frame["_source_version"] = version if not isinstance(version, pd.Series) else version.to_numpy()
    frame["_is_deleted"] = deleted if not isinstance(deleted, pd.Series) else deleted.to_numpy()
    name = f"_batch_{uuid.uuid4().hex[:8]}"
    con.register(name, frame)
    try:
        casts = ", ".join(
            f"TRY_CAST({q(c)} AS {t}) AS {q(c)}" if c in frame.columns else f"NULL::{t} AS {q(c)}"
            for c, t in columns.items()
        )
        con.execute(
            f"INSERT INTO {schema}.{q(table)} BY NAME SELECT {casts}, "
            "?::TIMESTAMP AS _loaded_at, ? AS _loaded_by, ? AS _source_system, "
            "CAST(_source_version AS BIGINT) AS _source_version, CAST(_is_deleted AS BOOLEAN) AS _is_deleted, "
            f"? AS _run_id FROM {name}",
            [run.loaded_at, run.loaded_by, source, run.run_id],
        )
    finally:
        con.unregister(name)
    return len(frame)
