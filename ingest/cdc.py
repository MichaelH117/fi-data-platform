"""Admin Core -> raw_admin_core: change-tracking replication (the SQL Server Change Tracking pattern).

Per table, pull ``row_version > watermark`` (net change since the last pull), append the rows to
``raw_admin_core.<table>`` with ``_source_version = row_version`` and ``_is_deleted =
removed_from_source = 1``, and advance the watermark in the same DuckDB transaction. A crash
leaves the watermark where it was, so the next run re-pulls the same versions (idempotent).

Every row is checked against the column types and NOT NULL flags declared in
``contracts/admin_core.yml`` (generated from the DDL: ``python -m ingest.cdc --write-contract``).
SQLite accepts any value in any column, and a value that cannot be cast to its declared type would
otherwise land in bronze as NULL. Such a row is not loaded: it goes to
``raw_admin_core._quarantine`` with ``reject_reason`` (``type:<column>`` / ``null:<column>``) and
``raw_json``, and is counted in ``audit.ingest_run.rows_quarantined`` (status ``warn``). The
watermark still moves, because a fix in the source stamps a new ``row_version`` that replicates on
the next run.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd
import yaml

from ingest import raw

log = logging.getLogger("ingest.cdc")
SOURCE = "admin_core"
SCHEMA = raw.RAW_SCHEMAS[SOURCE]
SQLITE_TO_DUCK = {"INTEGER": "BIGINT", "REAL": "DOUBLE", "TEXT": "VARCHAR", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP"}
QUARANTINE_COLS = {"table_name": "VARCHAR", "primary_key": "VARCHAR", "reject_reason": "VARCHAR",
                   "raw_json": "VARCHAR"}


def contract_tables(path: Path) -> dict[str, dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["tables"]


def write_contract(path: Path) -> None:
    """Regenerate the ``tables:`` block of the Admin Core contract from the DDL (keeps the header)."""
    from generator import admin_core as ac

    head = path.read_text(encoding="utf-8").split("\ntables:\n")[0]
    old = contract_tables(path)
    lines = [head, "tables:"]
    for table, cols in ac.column_specs().items():
        lines.append(f"  {table}:")
        lines.append(f"    primary_key: [{ac.pk(table)}]")
        pii = (old.get(table) or {}).get("pii") or ac.PII_COLUMNS.get(table)
        if pii:
            lines.append(f"    pii: [{', '.join(pii)}]")
        lines.append("    columns:")
        lines += [f"      {c}: {t}" for c, t in cols.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def contract_violations(con: duckdb.DuckDBPyConnection, df: pd.DataFrame, spec: dict[str, str]) -> pd.Series:
    """Reason per row ('' = valid): a value that would not survive the cast to its declared type, or a
    NULL in a NOT NULL column. SQLite hands numbers back as numbers, so only text values in typed
    columns can fail a cast; those go through DuckDB's TRY_CAST, the same cast ``raw.append`` uses."""
    reasons = pd.Series("", index=df.index, dtype=object)
    for col, decl in (spec or {}).items():
        if col not in df.columns:
            continue
        typ = SQLITE_TO_DUCK.get(str(decl).split()[0].upper(), "VARCHAR")
        vals = df[col]
        if typ != "VARCHAR":
            text = vals[vals.map(lambda v: isinstance(v, str))]
            if len(text):
                failed = con.execute(f"SELECT list_transform(?::VARCHAR[], x -> TRY_CAST(x AS {typ}) IS NULL)",
                                     [list(text)]).fetchone()[0]
                idx = text.index[failed]
                reasons.loc[idx] = reasons.loc[idx] + f";type:{col}"
        if str(decl).upper().endswith("NOT NULL"):
            idx = vals.index[vals.isna().to_numpy()]
            reasons.loc[idx] = reasons.loc[idx] + f";null:{col}"
    return reasons.str.lstrip(";")


def source_columns(src: sqlite3.Connection, table: str) -> dict[str, str]:
    cols = {}
    for _, name, decl, *_ in src.execute(f'PRAGMA table_info("{table}")'):
        cols[name] = SQLITE_TO_DUCK.get(str(decl).upper().split("(")[0], "VARCHAR")
    return cols


def replicate_table(con: duckdb.DuckDBPyConnection, src: sqlite3.Connection, table: str, run: raw.Run,
                    spec: dict[str, str] | None = None) -> raw.TableResult:
    res = raw.TableResult(SOURCE, table, watermark_from=raw.get_watermark(con, SOURCE, table))
    since = int(res.watermark_from or 0)
    cols = source_columns(src, table)
    if not cols:
        res.status, res.message = "error", "table missing in source"
        return res
    df = pd.read_sql(f'SELECT * FROM "{table}" WHERE row_version > ? ORDER BY row_version', src, params=[since])
    res.rows_read = len(df)
    if df.empty:
        res.watermark_to = res.watermark_from
        return res
    res.watermark_to = str(int(df["row_version"].max()))
    reasons = contract_violations(con, df, spec or {})
    bad = (reasons != "").to_numpy()
    con.execute("BEGIN")
    try:
        if bad.any():
            _quarantine(con, table, df[bad], reasons[bad], run, res)
            df = df[~bad]
        raw.ensure_table(con, SCHEMA, table, cols)
        res.rows_loaded = raw.append(con, SCHEMA, table, df, cols, run, SOURCE, df["row_version"],
                                     df["removed_from_source"].fillna(0).astype(int) == 1)
        res.rows_deleted = int((df["removed_from_source"].fillna(0).astype(int) == 1).sum())
        raw.set_watermark(con, SOURCE, table, "row_version", res.watermark_to, run)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return res


def _quarantine(con: duckdb.DuckDBPyConnection, table: str, q: pd.DataFrame, reasons: pd.Series, run: raw.Run,
                res: raw.TableResult) -> None:
    from generator import admin_core as ac

    key = ac.pk(table) if table in ac.TABLES and ac.pk(table) in q.columns else q.columns[0]
    records = q.astype(object).where(q.notna(), None).to_dict("records")
    qdf = pd.DataFrame({"table_name": table, "primary_key": q[key].astype(str).to_numpy(),
                        "reject_reason": reasons.to_numpy(),
                        "raw_json": [json.dumps(r, default=str, sort_keys=True) for r in records]})
    raw.ensure_table(con, SCHEMA, "_quarantine", QUARANTINE_COLS)
    raw.append(con, SCHEMA, "_quarantine", qdf, QUARANTINE_COLS, run, SOURCE, q["row_version"], False)
    counts = pd.Series([r for x in reasons for r in x.split(";")]).value_counts().sort_index()
    res.rows_quarantined = len(q)
    res.message = "quarantined by contract: " + ", ".join(f"{k}={v}" for k, v in counts.items())
    log.warning("%s.%s: %d rows not loaded, %s", SOURCE, table, len(q), res.message)


def run(con: duckdb.DuckDBPyConnection, source: Path, contract: Path, run_: raw.Run,
        tables: list[str] | None = None) -> list[raw.TableResult]:
    if not source.exists():
        raise FileNotFoundError(f"Admin Core not found: {source} (run `python -m generator`)")
    results = []
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as src:
        for table, meta in contract_tables(contract).items():
            if tables and table not in tables:
                continue
            started = datetime.now()
            try:
                res = replicate_table(con, src, table, run_, (meta or {}).get("columns"))
            except (sqlite3.Error, duckdb.Error) as exc:
                log.error("%s.%s: replication failed: %s", SOURCE, table, exc)
                res = raw.TableResult(SOURCE, table, watermark_from=raw.get_watermark(con, SOURCE, table),
                                      status="error", message=str(exc))
            raw.log_run(con, run_, res, started)
            results.append(res)
    return results


if __name__ == "__main__":
    import sys

    from ingest import ADMIN_CORE_CONTRACT

    if sys.argv[1:] != ["--write-contract"]:
        raise SystemExit("usage: python -m ingest.cdc --write-contract")
    write_contract(ADMIN_CORE_CONTRACT)
    print(f"wrote {ADMIN_CORE_CONTRACT}")
