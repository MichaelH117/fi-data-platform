"""Run the analyst SQL playbook (``sql/playbook/manifest.yml``) against the legacy databases.

Manifest shape (owned by the playbook author)::

    queries:
      - file: 01_catalog_discovery.sql
        database: legacy          # legacy | legacy_admin | modern
        timeout: 30s              # wall-clock cap for the whole file (the file's own `set` also applies)
        expected_rows: 12         # or [min, max]; omit to only require that it runs
        optional: true            # a missing database / relation is SKIP, not FAIL (the modern siblings)

Each file runs whole (``set statement_timeout``, ``explain``, discovery steps, then the answer) and the
row count of the LAST result set is checked. The runner enforces the manifest timeout with a client-side
cancel, so a file that raises its own ``statement_timeout`` still cannot run past the cap.

Results print as a table and are written to ``data/legacy/playbook_results.json`` and ``.md``.
Exit 0 when nothing failed (skips are fine), 1 on any failure, 2 when there is no manifest.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import yaml

from legacy import PLAYBOOK_DIR, STATE_DIR, databases
from legacy.benchmark import _db

DEFAULT_TIMEOUT = "60s"
RESULTS_JSON = STATE_DIR / "playbook_results.json"
RESULTS_MD = STATE_DIR / "playbook_results.md"
_HEADER = re.compile(r"^--\s*(pattern|question|trap)\s*:\s*(.+)$", re.I)
_UNITS = {"ms": 0.001, "s": 1, "sec": 1, "min": 60, "m": 60, "h": 3600}


def check(rows: int | None, expected) -> bool:
    if rows is None:
        return False
    if expected is None:
        return True
    if isinstance(expected, (list, tuple)):
        lo, hi = expected
        return (lo is None or rows >= lo) and (hi is None or rows <= hi)
    return rows == int(expected)


def seconds(timeout: str | int | float) -> float:
    """'30s' / '5min' / '1500ms' / 90 -> seconds."""
    if isinstance(timeout, (int, float)):
        return float(timeout)
    m = re.fullmatch(r"\s*([\d.]+)\s*([a-z]*)\s*", str(timeout).lower())
    if not m or (m.group(2) and m.group(2) not in _UNITS):
        raise ValueError(f"bad timeout {timeout!r}")
    return float(m.group(1)) * _UNITS.get(m.group(2) or "s", 1)


def header(sql: str) -> dict[str, str]:
    """The pattern / question / trap header lines, continuation lines folded in."""
    out: dict[str, str] = {}
    key = None
    for line in sql.splitlines():
        if not line.startswith("--"):
            break
        if m := _HEADER.match(line):
            key = m.group(1).lower()
            out[key] = m.group(2).strip()
        elif key and line[2:].startswith(" " * 3) and line[2:].strip():
            out[key] += " " + line[2:].strip()
        else:
            key = None
    return out


def execute(settings, database: str, sql: str, timeout: str) -> dict:
    """Run one file; return rows of the last result set, its columns and first row, elapsed ms."""
    import psycopg

    res: dict = {"database": database, "timeout": str(timeout), "timed_out": False, "error": None, "rows": None}
    t0 = time.perf_counter()
    try:
        con = settings.connect(database=database, autocommit=True, reader=False)
    except psycopg.Error as exc:
        res["error"] = "connect: " + str(exc).strip().splitlines()[0]
        res["missing"] = True
        res["ms"] = 0.0
        return res
    with con:
        timer = threading.Timer(seconds(timeout), getattr(con, "cancel_safe", con.cancel))
        timer.start()
        try:
            cur = con.execute(sql)
            rows: list[tuple] = []
            cols: list[str] = []
            while True:
                if cur.description is not None:
                    rows, cols = cur.fetchall(), [d.name for d in cur.description]
                if not cur.nextset():
                    break
            res["rows"], res["columns"] = len(rows), cols
            res["first_row"] = [str(v) for v in rows[0]] if rows else None
        except psycopg.errors.QueryCanceled:
            res["timed_out"] = True
        except (psycopg.errors.UndefinedTable, psycopg.errors.InvalidSchemaName) as exc:
            res["error"], res["missing"] = str(exc).strip().splitlines()[0], True
        except psycopg.Error as exc:
            res["error"] = str(exc).strip().splitlines()[0]
        finally:
            timer.cancel()
    res["ms"] = round((time.perf_counter() - t0) * 1000, 1)
    return res


def run(manifest: Path, settings=None) -> list[dict]:
    from warehouse.postgres.sync import Settings

    s = settings or Settings.from_env()
    legacy_db, admin_db = databases()
    spec = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    results = []
    for q in spec.get("queries", []):
        f = manifest.parent / q["file"]
        db = _db(q.get("database", "legacy"), legacy_db, admin_db, s.database)
        base = {"file": q["file"], "database": db, "expected_rows": q.get("expected_rows")}
        if not f.exists():
            results.append(base | {"ok": False, "status": "FAIL", "error": "file not found", "rows": None})
            continue
        sql = f.read_text(encoding="utf-8")
        r = base | header(sql) | execute(s, db, sql, str(q.get("timeout", DEFAULT_TIMEOUT)))
        if q.get("optional") and r.pop("missing", False):
            r["ok"], r["status"] = True, "SKIP"
        else:
            r.pop("missing", None)
            r["ok"] = r["error"] is None and not r["timed_out"] and check(r.get("rows"), q.get("expected_rows"))
            r["status"] = "PASS" if r["ok"] else "FAIL"
        results.append(r)
    return results


def _note(r: dict) -> str:
    if r.get("timed_out"):
        return f"timeout after {r['timeout']}"
    if r.get("error"):
        return r["error"]
    exp = r.get("expected_rows")
    return "" if r["ok"] or exp is None else f"expected {exp}"


def table_md(results: list[dict]) -> str:
    lines = ["| status | file | database | rows | expected | ms | note |", "|---|---|---|---:|---|---:|---|"]
    for r in results:
        note = _note(r).replace("|", "/")
        rows = "-" if r.get("rows") is None else r["rows"]
        exp = "any" if r.get("expected_rows") is None else r["expected_rows"]
        lines.append(f"| {r['status']} | {r['file']} | {r['database']} | {rows} | {exp} | {r.get('ms', 0)} | {note} |")
    return "\n".join(lines) + "\n"


def write(results: list[dict], json_path: Path = RESULTS_JSON, md_path: Path = RESULTS_MD) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "passed": sum(r["status"] == "PASS" for r in results),
               "skipped": sum(r["status"] == "SKIP" for r in results),
               "failed": sum(r["status"] == "FAIL" for r in results), "results": results}
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    md_path.write_text(f"# Playbook results\n\nGenerated {payload['generated_at']}: {payload['passed']} passed, "
                       f"{payload['skipped']} skipped, {payload['failed']} failed.\n\n{table_md(results)}",
                       encoding="utf-8")


def main(manifest: Path | None = None) -> int:
    manifest = manifest or PLAYBOOK_DIR / "manifest.yml"
    if not manifest.exists():
        print(f"legacy playbook: {manifest} not found (the playbook is added by its owner)", file=sys.stderr)
        return 2
    results = run(manifest)
    for r in results:
        rows = "-" if r.get("rows") is None else r["rows"]
        print(f"{r['status']:<5} {r['file']:<44} {r['database']:<18} {r.get('ms', 0):>9} ms  rows={rows:<6} {_note(r)}")
    write(results)
    failed = sum(r["status"] == "FAIL" for r in results)
    skipped = sum(r["status"] == "SKIP" for r in results)
    print(f"playbook: {len(results) - failed - skipped}/{len(results)} passed, {skipped} skipped, {failed} failed"
          f" -> {RESULTS_MD}")
    return 1 if failed else 0
