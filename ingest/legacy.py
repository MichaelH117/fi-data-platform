"""Legacy admin -> raw_legacy_admin: ``updated_at`` watermark + data contract + key diff for deletes.

The legacy system has no change tracking, so each table is pulled with
``updated_at >= watermark - LOOKBACK`` (rows already loaded in that window are skipped by
fingerprint, see ``audit.ingest_row_fingerprint``) and validated against
``contracts/legacy_admin.yml``: rows that break the contract go to ``raw_legacy_admin._quarantine``
(raw JSON kept for replay), valid rows are appended with
``_source_version`` = epoch seconds of ``updated_at``. Because a watermark never sees a hard delete,
every run also diffs the source keys against the latest raw keys and appends a tombstone
(``_is_deleted = true``) for any key that disappeared.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

from ingest import raw
from ingest.contract import DUCKDB_TYPES, SchemaViolation, TableContract, load_contract, validate

log = logging.getLogger("ingest.legacy")
SOURCE = "legacy_admin"
SCHEMA = raw.RAW_SCHEMAS[SOURCE]
LOOKBACK = timedelta(minutes=5)
QUARANTINE_COLS = {"table_name": "VARCHAR", "reject_reason": "VARCHAR", "raw_json": "VARCHAR"}


def _epoch(ts: pd.Series) -> pd.Series:
    t = pd.to_datetime(ts, format="%Y-%m-%d %H:%M:%S", errors="coerce")
    return (t.astype("int64") // 10**9).where(t.notna(), 0)


def _lookback(since: str | None) -> str | None:
    """Start of the re-read window: the watermark minus LOOKBACK (the watermark itself if unparseable)."""
    if since is None:
        return None
    try:
        return (datetime.strptime(since[:19], "%Y-%m-%d %H:%M:%S") - LOOKBACK).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return since


def _extract(src: sqlite3.Connection, table: str, since: str | None) -> tuple[pd.DataFrame, pd.Series]:
    """Rows with ``updated_at >= since`` (all rows when None) plus a fingerprint per row (hash of the
    row exactly as the source returned it), so a re-read window never loads the same row twice."""
    sql = f'SELECT * FROM "{table}"'  # table names come from the contract
    if since is None:
        cur = src.execute(sql + " ORDER BY updated_at")
    else:
        cur = src.execute(sql + " WHERE updated_at >= ? ORDER BY updated_at", [since])
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    fps = pd.Series([hashlib.md5(repr(r).encode()).hexdigest() for r in rows], dtype="object")
    return pd.DataFrame.from_records(rows, columns=cols, coerce_float=True), fps


def _seen(con, table: str) -> set[str]:
    return {r[0] for r in con.execute("SELECT fingerprint FROM audit.ingest_row_fingerprint "
                                      "WHERE source_system = ? AND table_name = ?", [SOURCE, table]).fetchall()}


def _remember(con, table: str, extract: pd.DataFrame, fps: pd.Series, watermark: str | None) -> None:
    """Keep the fingerprints of every row inside the next run's look-back window."""
    con.execute("DELETE FROM audit.ingest_row_fingerprint WHERE source_system = ? AND table_name = ?",
                [SOURCE, table])
    start = _lookback(watermark)
    if start is None or extract.empty:
        return
    upd = extract["updated_at"].astype("string")
    keep = (upd >= start).fillna(False).to_numpy()
    if keep.any():
        con.executemany("INSERT INTO audit.ingest_row_fingerprint VALUES (?, ?, ?, ?)",
                        [(SOURCE, table, str(u), f) for u, f in zip(upd[keep], fps[keep], strict=True)])


def _key_diff(con, src: sqlite3.Connection, tc: TableContract, cols: dict[str, str], run: raw.Run) -> int:
    """Tombstone keys that are in raw (latest row not deleted) but no longer in the source."""
    pk = list(tc.primary_key)
    key_sql = ", ".join(raw.q(k) for k in pk)
    src_keys = pd.read_sql(f'SELECT DISTINCT {key_sql} FROM "{tc.name}"', src)
    if src_keys.empty:  # never wipe a table because an extract came back empty
        return 0
    latest = con.execute(
        f"SELECT {key_sql} FROM (SELECT {key_sql}, _is_deleted, row_number() OVER (PARTITION BY {key_sql} "
        f"ORDER BY _source_version DESC, _loaded_at DESC) AS rn FROM {SCHEMA}.{raw.q(tc.name)}) "
        "WHERE rn = 1 AND NOT coalesce(_is_deleted, false)"
    ).df()
    for k in pk:
        typ = DUCKDB_TYPES[next(c.type for c in tc.columns if c.name == k)]
        if typ in ("BIGINT", "DOUBLE"):
            src_keys[k] = pd.to_numeric(src_keys[k], errors="coerce")
            latest[k] = pd.to_numeric(latest[k], errors="coerce")
        else:
            src_keys[k], latest[k] = src_keys[k].astype("string"), latest[k].astype("string")
    gone = latest.merge(src_keys, on=pk, how="left", indicator=True)
    gone = gone[gone["_merge"] == "left_only"][pk]
    if gone.empty:
        return 0
    version = int(run.loaded_at.timestamp())
    return raw.append(con, SCHEMA, tc.name, gone, cols, run, SOURCE, version, True)


def load_table(con, src: sqlite3.Connection, tc: TableContract, run: raw.Run) -> raw.TableResult:
    res = raw.TableResult(SOURCE, tc.name, watermark_from=raw.get_watermark(con, SOURCE, tc.name))
    cols = {c.name: DUCKDB_TYPES[c.type] for c in tc.columns}
    # Re-read a LOOKBACK window before the watermark: a row committed later in the same second as the
    # last pull (timestamps are second-resolution and heavily tied) is still picked up. Rows already
    # loaded are recognised by fingerprint and skipped; staging keeps the latest version per key.
    extract, fps = _extract(src, tc.name, _lookback(res.watermark_from))
    seen = _seen(con, tc.name) if res.watermark_from is not None else set()
    new = ~fps.isin(seen).to_numpy()
    batch = extract[new].reset_index(drop=True)
    res.rows_read = len(batch)
    con.execute("BEGIN")
    try:
        raw.ensure_table(con, SCHEMA, tc.name, cols)
        raw.ensure_table(con, SCHEMA, "_quarantine", QUARANTINE_COLS)
        if len(batch):
            checked = validate(batch, tc)
            for w in checked.warnings:
                log.warning("%s.%s: %s", SOURCE, tc.name, w)
            top = str(batch["updated_at"].dropna().max())
            res.watermark_to = top if res.watermark_from is None or top > res.watermark_from else res.watermark_from
            valid = checked.valid
            # Typed timestamps back to text for the epoch; valid rows keep the source order.
            version = _epoch(valid["updated_at"].dt.strftime("%Y-%m-%d %H:%M:%S"))
            deleted = valid["removed_from_source"].fillna(0).astype(int) == 1 if "removed_from_source" in valid \
                else False
            res.rows_loaded = raw.append(con, SCHEMA, tc.name, valid, cols, run, SOURCE, version, deleted)
            q = checked.quarantined
            if len(q):
                records = q.drop(columns="_reason").astype(object).where(q.notna(), None).to_dict("records")
                qdf = pd.DataFrame({"table_name": tc.name, "reject_reason": q["_reason"].to_numpy(),
                                    "raw_json": [json.dumps(r, default=str, sort_keys=True) for r in records]})
                raw.append(con, SCHEMA, "_quarantine", qdf, QUARANTINE_COLS, run, SOURCE,
                           _epoch(q["updated_at"].astype("string")), False)
                res.rows_quarantined = len(q)
                log.warning("%s.%s: %d rows quarantined (%s)", SOURCE, tc.name, len(q),
                            ", ".join(sorted(set(q["_reason"])))[:200])
            raw.set_watermark(con, SOURCE, tc.name, "updated_at", res.watermark_to, run)
        else:
            res.watermark_to = res.watermark_from
        _remember(con, tc.name, extract, fps, res.watermark_to)
        res.rows_deleted = _key_diff(con, src, tc, cols, run)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return res


def run(con: duckdb.DuckDBPyConnection, source: Path, contract: Path, run_: raw.Run,
        tables: list[str] | None = None) -> list[raw.TableResult]:
    if not source.exists():
        raise FileNotFoundError(f"legacy admin database not found: {source} (run `python -m generator`)")
    spec = load_contract(contract)
    results = []
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as src:
        for name, tc in spec.items():
            if tables and name not in tables:
                continue
            started = datetime.now()
            try:
                res = load_table(con, src, tc, run_)
            except (SchemaViolation, sqlite3.Error, duckdb.Error) as exc:
                log.error("%s.%s: batch rejected: %s", SOURCE, name, exc)
                res = raw.TableResult(SOURCE, name, watermark_from=raw.get_watermark(con, SOURCE, name),
                                      status="error", message=str(exc))
            raw.log_run(con, run_, res, started)
            results.append(res)
    return results
