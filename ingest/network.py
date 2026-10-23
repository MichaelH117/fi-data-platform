"""Landing files -> raw_network_feed: untouched copy of every delivered file, plus manifest and quarantine.

- ``raw_network_feed.deals`` / ``remittance`` / ``econtract_events``: every row of every file, as
  text, with ``_source_file``, ``_file_sha256``, ``_row_number``, ``_feed_date`` and
  ``_source_version`` = the drop's sequence number. Rows from rejected files are copied too (raw
  keeps everything); staging removes them by anti-joining ``_quarantine`` on
  (``_source_file``, ``_row_number``).
- ``raw_network_feed._manifest``: one row per file and drop, with the checksum and row-count checks.
- ``raw_network_feed._quarantine``: rows the intake rejected (from Admin Core ``intake_reject``),
  with ``reject_reason`` and ``raw_json``. Never promoted.
Each loaded drop is logged in ``audit.ingest_drop`` (sequence number + manifest sha256), and a drop
loads when its sequence number is not in that set, so a drop is copied exactly once, and a late,
out-of-order or repaired drop still loads after a later one. A folder whose manifest is missing or
unreadable is logged as ``unreadable`` and retried every run; :func:`drop_problems` (and the
same rule in the ``diag_freshness`` gate) fails while any drop is unloaded or the loaded sequence
numbers have a gap. The watermark (highest
sequence loaded) is kept for monitoring only.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

from generator.landing import FILES, MANIFEST, sha256
from ingest import raw
from ingest.feed import read_rows

log = logging.getLogger("ingest.network")
SOURCE = "network_feed"
SCHEMA = raw.RAW_SCHEMAS[SOURCE]
FILE_META = {"_source_file": "VARCHAR", "_file_sha256": "VARCHAR", "_row_number": "BIGINT", "_feed_date": "DATE"}
MANIFEST_COLS = {"feed_date": "DATE", "sequence_no": "BIGINT", "file_name": "VARCHAR", "expected_rows": "BIGINT",
                 "actual_rows": "BIGINT", "expected_sha256": "VARCHAR", "actual_sha256": "VARCHAR",
                 "checksum_ok": "BOOLEAN", "row_count_ok": "BOOLEAN", "generated_at": "TIMESTAMP"}
QUARANTINE_COLS = {"reject_id": "BIGINT", "tenant_id": "BIGINT", "file_name": "VARCHAR", "reject_reason": "VARCHAR",
                   "raw_json": "VARCHAR", **FILE_META}


def _columns(df: pd.DataFrame) -> dict[str, str]:
    return {**{c: "VARCHAR" for c in df.columns}, **FILE_META}


def _drops(landing: Path) -> tuple[list[tuple[int, Path, dict, str]], list[Path]]:
    """(readable drops sorted by sequence number with their manifest sha256, unreadable folders)."""
    out, bad = [], []
    for folder in sorted(p for p in landing.iterdir() if p.is_dir()) if landing.exists() else []:
        try:
            m = json.loads((folder / MANIFEST).read_text(encoding="utf-8"))
            seq = int(m["sequence_no"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            log.error("%s: missing or unreadable manifest; not loaded, retried on every run until it is fixed",
                      folder.name)
            bad.append(folder)
            continue
        out.append((seq, folder, m, sha256(folder / MANIFEST)))
    return sorted(out, key=lambda x: x[0]), bad


LOADED_SQL = f"SELECT DISTINCT strftime(feed_date, '%Y-%m-%d'), sequence_no FROM {SCHEMA}._manifest"


def load_files(con: duckdb.DuckDBPyConnection, landing: Path, run: raw.Run) -> list[raw.TableResult]:
    results = {k: raw.TableResult(SOURCE, k) for k in [*FILES, "_manifest"]}
    for k, r in results.items():
        r.watermark_from = r.watermark_to = raw.get_watermark(con, SOURCE, k)
    con.execute("BEGIN")
    try:
        raw.ensure_table(con, SCHEMA, "_manifest", MANIFEST_COLS)
        loaded = raw.loaded_drops(con, SOURCE, LOADED_SQL)
        drops, unreadable = _drops(landing)
        for folder in unreadable:
            raw.record_drop(con, SOURCE, folder.name, None, None, "unreadable", run)
        for seq, folder, manifest, manifest_sha in drops:
            if seq in loaded:
                continue
            loaded.add(seq)
            rows = []
            for key, name in FILES.items():
                path = folder / name
                exp = manifest.get("files", {}).get(name, {})
                if not path.exists():
                    rows.append({"file_name": name, "actual_rows": None, "actual_sha256": None, **_exp(exp)})
                    continue
                df = read_rows(path)
                digest = sha256(path)
                rel = f"{folder.name}/{name}"
                meta = pd.DataFrame({"_source_file": rel, "_file_sha256": digest,
                                     "_row_number": range(1, len(df) + 1), "_feed_date": folder.name})
                frame = pd.concat([df.reset_index(drop=True), meta], axis=1)
                cols = _columns(df)
                raw.ensure_table(con, SCHEMA, key, cols)
                res = results[key]
                res.rows_read += len(df)
                res.rows_loaded += raw.append(con, SCHEMA, key, frame, cols, run, SOURCE, seq, False)
                res.watermark_to = str(max(seq, int(res.watermark_to or 0)))
                rows.append({"file_name": name, "actual_rows": len(df), "actual_sha256": digest, **_exp(exp)})
            m = pd.DataFrame(rows)
            m["feed_date"], m["sequence_no"], m["generated_at"] = folder.name, seq, manifest.get("generated_at")
            m["checksum_ok"] = m["actual_sha256"] == m["expected_sha256"]
            m["row_count_ok"] = m["actual_rows"] == m["expected_rows"]
            results["_manifest"].rows_read += len(m)
            results["_manifest"].rows_loaded += raw.append(con, SCHEMA, "_manifest", m, MANIFEST_COLS, run, SOURCE,
                                                           seq, False)
            raw.record_drop(con, SOURCE, folder.name, seq, manifest_sha, "loaded", run)
            results["_manifest"].watermark_to = str(max(seq, int(results["_manifest"].watermark_to or 0)))
        for k, r in results.items():
            if r.watermark_to != r.watermark_from and r.watermark_to is not None:
                raw.set_watermark(con, SOURCE, k, "sequence_no", r.watermark_to, run)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return list(results.values())


def _exp(exp: dict) -> dict:
    return {"expected_rows": exp.get("row_count"), "expected_sha256": exp.get("sha256")}



def drop_problems(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Lost-drop findings over every file source: a gap in the loaded sequence numbers (1..max), or a
    drop folder seen but never loaded. Empty = nothing lost. Mirrored by ``diag_freshness``."""
    gaps = con.execute("""
        WITH loaded AS (SELECT DISTINCT source_system, sequence_no FROM audit.ingest_drop
                        WHERE status = 'loaded' AND sequence_no IS NOT NULL),
             expected AS (SELECT source_system, unnest(generate_series(1, max(sequence_no))) AS sequence_no
                          FROM loaded GROUP BY source_system)
        SELECT 'sequence gap: ' || e.source_system || ' #' || e.sequence_no
        FROM expected e LEFT JOIN loaded l USING (source_system, sequence_no) WHERE l.sequence_no IS NULL
        UNION ALL
        SELECT DISTINCT 'landing drop not loaded: ' || u.source_system || '/' || u.drop_name
        FROM audit.ingest_drop u
        WHERE u.status <> 'loaded' AND NOT EXISTS (
            SELECT 1 FROM audit.ingest_drop l WHERE l.status = 'loaded' AND l.source_system = u.source_system
            AND l.drop_name = u.drop_name)
    """).fetchall()
    return sorted(r[0] for r in gaps)

def load_quarantine(con: duckdb.DuckDBPyConnection, run: raw.Run) -> raw.TableResult:
    """Copy this run's newly replicated intake rejects into ``raw_network_feed._quarantine``."""
    res = raw.TableResult(SOURCE, "_quarantine")
    have = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema = 'raw_admin_core' "
                       "AND table_name IN ('intake_reject', 'intake_file')").fetchone()[0]
    raw.ensure_table(con, SCHEMA, "_quarantine", QUARANTINE_COLS)
    if have < 2:
        return res
    df = con.execute(
        """
        SELECT r.reject_id, r.tenant_id, r.file_name, r.reject_reason, r.raw_json,
               strftime(r.feed_date, '%Y-%m-%d') || '/' || r.file_name AS _source_file,
               f.actual_sha256 AS _file_sha256, r.row_number AS _row_number, r.feed_date AS _feed_date,
               r.row_version
        FROM raw_admin_core.intake_reject r
        LEFT JOIN (SELECT intake_file_id, any_value(actual_sha256) AS actual_sha256
                   FROM raw_admin_core.intake_file GROUP BY 1) f USING (intake_file_id)
        WHERE r._run_id = ?
        """,
        [run.run_id],
    ).df()
    res.rows_read = len(df)
    if len(df):
        res.rows_loaded = raw.append(con, SCHEMA, "_quarantine", df, QUARANTINE_COLS, run, SOURCE,
                                     df["row_version"], False)
    return res


def run(con: duckdb.DuckDBPyConnection, landing: Path, run_: raw.Run) -> list[raw.TableResult]:
    started = datetime.now()
    try:
        results = load_files(con, landing, run_)
    except (OSError, duckdb.Error, ValueError) as exc:
        log.error("network feed raw copy failed: %s", exc)
        results = [raw.TableResult(SOURCE, "files", status="error", message=str(exc))]
    results.append(load_quarantine(con, run_))
    for r in results:
        raw.log_run(con, run_, r, started)
    return results
