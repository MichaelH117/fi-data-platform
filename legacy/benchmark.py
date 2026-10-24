"""Run the benchmark questions both ways and write ``site/data/legacy_benchmark.json`` (V3_DESIGN 8.7).

Each question is a set of files in ``sql/playbook/benchmark/``: ``qNN_legacy.sql`` (plus optional extra
legacy steps ``qNN_legacy_2.sql``, ``qNN_legacy_3.sql`` ... run in order, for answers that take more than
one query) and ``qNN_modern.sql`` (run against the synced marts in ``PG_DATABASE``). The same server
answers both, so the timings compare. Header lines a file may carry:

    -- database: legacy | legacy_admin | modern
    -- timeout: 30s
    -- expect_error: cross-database     (this step is EXPECTED to fail with an error containing the text)
    -- question: ... / -- trap: ...      (shown in the benchmark doc and on the site page)

Per step the JSON records the answer row (columns + first row), the headline answer (first numeric value
of the first row, else the row count), elapsed ms (best of two when the query is fast), a timeout flag
("timed out at N s"), and the top plan nodes. The plan is ``EXPLAIN (ANALYZE, BUFFERS)`` when the query
finished inside ``ANALYZE_MAX_S``; a query that ran longer or timed out only gets a plain ``EXPLAIN``,
so nothing is ever re-run past its budget. The statement timeout is enforced twice: the server-side
``statement_timeout`` and a client-side cancel at the same cap.
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from legacy import BENCHMARK_JSON, PLAYBOOK_DIR, STATE_DIR, databases

QUESTIONS: dict[str, tuple[str, str]] = {  # id -> (business question, expected legacy failure class)
    "q01": ("In-force contracts and coverage cost for one dealer", "lost"),
    "q02": ("Contracts by product type, latest plan year", "lost"),
    "q03": ("Paid claims by month", "sign"),
    "q04": ("Book for one provider entity", "zeroed"),
    "q05": ("Loss ratio by entered year from earnings", "skewed"),
    "q06": ("Reportable population for a compliance filing", "doubled"),
    "q07": ("Attributes for a provider-scoped population", "timeout"),
    "q08": ("Tenant B in-force count", "stale"),
    "q09": ("RV chassis make/model per contract", "extra step"),
    "q10": ("Stock number match rate by VIN", "lost"),
}
DEFAULT_TIMEOUT = "60s"
ANALYZE_MAX_S = 60.0  # never EXPLAIN ANALYZE a query that ran (or could run) longer than this
REPEAT_UNDER_S = 5.0  # fast queries run twice; the faster (warm-cache) timing is kept
STATE_JSON = STATE_DIR / "legacy_benchmark.json"  # survives a site rebuild (site/ is wiped by reports)
HEADER = re.compile(r"^--\s*(database|timeout|expect_error)\s*:\s*(.+?)\s*$", re.I | re.M)
TEXT_HEADER = re.compile(r"^--\s*(question|trap)\s*:\s*(.+)$", re.I)
_UNITS = {"ms": 0.001, "s": 1, "sec": 1, "min": 60, "m": 60, "h": 3600}


def headers(sql: str) -> dict[str, str]:
    return {k.lower(): v for k, v in HEADER.findall(sql)}


def text_headers(sql: str) -> dict[str, str]:
    """question / trap header lines, with indented continuation lines folded in."""
    out: dict[str, str] = {}
    key = None
    for line in sql.splitlines():
        if not line.startswith("--"):
            break
        if m := TEXT_HEADER.match(line):
            key = m.group(1).lower()
            out[key] = m.group(2).strip()
        elif key and line[2:].startswith("   ") and line[2:].strip():
            out[key] += " " + line[2:].strip()
        else:
            key = None
    return out


def seconds(timeout: str | int | float) -> float:
    if isinstance(timeout, (int, float)):
        return float(timeout)
    m = re.fullmatch(r"\s*([\d.]+)\s*([a-z]*)\s*", str(timeout).lower())
    if not m or (m.group(2) and m.group(2) not in _UNITS):
        raise ValueError(f"bad timeout {timeout!r}")
    return float(m.group(1)) * _UNITS.get(m.group(2) or "s", 1)


def last_statement(sql: str) -> str:
    """The final statement of a file (the one whose result is the answer), without trailing semicolons."""
    body = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    parts = [p.strip() for p in body.split(";") if p.strip()]
    return parts[-1] if parts else ""


def _answer(rows: list[tuple]) -> float | int:
    if rows:
        for v in rows[0]:
            if isinstance(v, (int, float, Decimal)) and not isinstance(v, bool):
                return float(v)
    return len(rows)


def _jsonable(v):
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return str(v)


def _plan_nodes(plan: dict, limit: int = 6) -> list[str]:
    """Breadth-first node labels; with ANALYZE each label carries actual rows and ms."""
    out, stack = [], [plan]
    while stack and len(out) < limit:
        node = stack.pop(0)
        label = node.get("Node Type", "?")
        if node.get("Relation Name"):
            label += f" on {node.get('Schema', '') + '.' if node.get('Schema') else ''}{node['Relation Name']}"
        if node.get("Index Name"):
            label += f" using {node['Index Name']}"
        if "Actual Rows" in node:
            label += f" (rows={int(node['Actual Rows'] * node.get('Actual Loops', 1)):,}, " \
                     f"{node.get('Actual Total Time', 0):,.0f} ms)"
        out.append(label)
        stack.extend(node.get("Plans", []))
    return out


def _scan_kinds(plan: dict) -> dict[str, int]:
    """How many of each scan type the plan uses (Seq Scan vs index / bitmap scans)."""
    kinds: dict[str, int] = {}
    stack = [plan]
    while stack:
        node = stack.pop()
        t = node.get("Node Type", "")
        if "Scan" in t:
            kinds[t] = kinds.get(t, 0) + 1
        stack.extend(node.get("Plans", []))
    return kinds


def _execute(con, sql: str, cap_s: float):
    """Run every statement of ``sql``; return (columns, rows) of the last result set. Client-side cancel at cap."""
    timer = threading.Timer(cap_s + 0.5, getattr(con, "cancel_safe", con.cancel))
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
        return cols, rows
    finally:
        timer.cancel()


def run_sql(settings, database: str, sql: str, timeout: str = DEFAULT_TIMEOUT,
            expect_error: str | None = None) -> dict:
    """Execute a file under its timeout; time it; EXPLAIN its final statement (ANALYZE only when it was quick)."""
    import psycopg

    cap = seconds(timeout)
    res: dict = {"database": database, "timeout": timeout, "timed_out": False, "error": None,
                 "expected_error": expect_error, "rows": None, "answer": None, "columns": [], "first_row": None}
    try:
        con = settings.connect(database=database, autocommit=True, reader=False)
    except psycopg.Error as exc:
        res["error"], res["ms"] = "connect: " + str(exc).strip().splitlines()[0], 0.0
        return res
    with con:
        con.execute(f"set statement_timeout = '{int(cap * 1000)}ms'")
        final = last_statement(sql)
        try:
            plan = con.execute(f"explain (format json) {final}").fetchone()[0][0]["Plan"]
            res["plan_top"], res["plan_cost"] = _plan_nodes(plan), plan.get("Total Cost")
            res["plan_scans"], res["plan_kind"] = _scan_kinds(plan), "estimated"
        except psycopg.Error:
            res["plan_top"], res["plan_kind"] = [], None
        timings = []
        for _attempt in range(2):
            t0 = time.perf_counter()
            try:
                cols, rows = _execute(con, sql, cap)
                timings.append((time.perf_counter() - t0) * 1000)
                res["rows"], res["answer"], res["columns"] = len(rows), _answer(rows), cols
                res["first_row"] = [_jsonable(v) for v in rows[0]] if rows else None
            except psycopg.errors.QueryCanceled:
                timings.append((time.perf_counter() - t0) * 1000)
                res["timed_out"] = True
                res["note"] = f"timed out at {cap:g} s"
                break
            except psycopg.Error as exc:
                timings.append((time.perf_counter() - t0) * 1000)
                res["error"] = str(exc).strip().splitlines()[0]
                break
            if timings[-1] / 1000 >= REPEAT_UNDER_S:
                break
        res["ms"] = round(min(timings), 1)
        if expect_error and res["error"] and expect_error.lower() in res["error"].lower():
            res["error_expected"], res["error_text"], res["error"] = True, res["error"], None
        if not res["timed_out"] and not res.get("error_text") and res["error"] is None \
                and res["ms"] / 1000 < min(ANALYZE_MAX_S, cap):
            try:
                plan = con.execute(f"explain (analyze, buffers, format json) {final}").fetchone()[0][0]
                res["plan_top"], res["plan_scans"] = _plan_nodes(plan["Plan"]), _scan_kinds(plan["Plan"])
                res["plan_kind"], res["plan_exec_ms"] = "analyze", round(plan.get("Execution Time", 0), 1)
            except psycopg.Error:
                pass
        elif res["timed_out"]:
            res["plan_kind"] = "estimated (no ANALYZE: the query hit its timeout)"
    return res


def _db(kind: str, legacy_db: str, admin_db: str, modern_db: str) -> str:
    return {"legacy": legacy_db, "legacy_admin": admin_db, "modern": modern_db}.get(kind, kind)


def legacy_steps(bench_dir: Path, qid: str) -> list[Path]:
    """qNN_legacy.sql, then qNN_legacy_2.sql, qNN_legacy_3.sql ... in numeric order."""
    first = bench_dir / f"{qid}_legacy.sql"
    extra = sorted(bench_dir.glob(f"{qid}_legacy_*.sql"), key=lambda p: int(re.sub(r"\D", "", p.stem[-2:]) or 0))
    return ([first] if first.exists() else []) + extra


def _rel(p: Path) -> str:
    try:
        return p.relative_to(PLAYBOOK_DIR.parent.parent).as_posix()
    except ValueError:
        return p.as_posix()


def classify(entry: dict, failure: str) -> str:
    leg, mod = entry["legacy"], entry["modern"]
    if leg.get("timed_out"):
        return "timeout"
    if leg.get("error") or mod.get("error"):
        return "error"
    if failure == "timeout":  # finished inside the budget: report how much slower it was, not a timeout
        return "slow" if (leg.get("ms") or 0) >= 10 * max(mod.get("ms") or 0, 0.1) else "none"
    if entry.get("diff") in (0, 0.0) and not any(s.get("error_expected") for s in entry.get("legacy_steps", [])):
        return "none"
    return failure


def run(settings=None, bench_dir: Path | None = None) -> dict:
    from warehouse.postgres.sync import Settings

    s = settings or Settings.from_env()
    legacy_db, admin_db = databases()
    bench_dir = bench_dir or PLAYBOOK_DIR / "benchmark"
    out = {"generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "legacy_database": legacy_db,
           "legacy_admin_database": admin_db, "modern_database": s.database, "analyze_max_s": ANALYZE_MAX_S,
           "questions": [], "skipped": []}
    for qid, (question, failure) in QUESTIONS.items():
        steps, mf = legacy_steps(bench_dir, qid), bench_dir / f"{qid}_modern.sql"
        if not (steps and mf.exists()):
            out["skipped"].append(qid)
            continue
        entry: dict = {"id": qid, "question": question, "expected_failure": failure}
        runs = []
        for f in steps:
            sql = f.read_text(encoding="utf-8")
            h = headers(sql)
            r = run_sql(s, _db(h.get("database", "legacy"), legacy_db, admin_db, s.database), sql,
                        h.get("timeout", DEFAULT_TIMEOUT), h.get("expect_error"))
            r["file"] = _rel(f)
            runs.append(r | {"text": text_headers(sql)})
        legacy = dict(runs[-1])
        legacy["ms"] = round(sum(r["ms"] for r in runs), 1)
        legacy["text"] = runs[0]["text"]
        if len(runs) > 1:
            entry["legacy_steps"] = runs
        sql = mf.read_text(encoding="utf-8")
        h = headers(sql)
        modern = run_sql(s, _db(h.get("database", "modern"), legacy_db, admin_db, s.database), sql,
                         h.get("timeout", DEFAULT_TIMEOUT), h.get("expect_error"))
        modern["file"], modern["text"] = _rel(mf), text_headers(sql)
        entry["legacy"], entry["modern"] = legacy, modern
        la, ma = legacy.get("answer"), modern.get("answer")
        num = isinstance(la, (int, float)) and isinstance(ma, (int, float))
        entry["diff"] = round(la - ma, 4) if num else None
        entry["diff_pct"] = round(100 * (la - ma) / ma, 2) if num and ma else None
        entry["observed_failure"] = classify(entry, failure)
        out["questions"].append(entry)
    out["clutter"] = clutter(legacy_db, s.database)
    return out


def clutter(legacy_db: str, modern_db: str) -> dict:
    """Schema-registry findings (pipeline.clutter_check) for both databases: pain point 10's measure."""
    try:
        from pipeline.clutter_check import check_postgres, load_registry

        reg = load_registry()
    except Exception as exc:  # the registry belongs to the foundations work; absent means not measured
        return {"error": str(exc)}
    out: dict = {}
    for side, db in (("legacy", legacy_db), ("modern", modern_db)):
        try:
            found, reason = check_postgres(reg, db)
        except Exception as exc:
            out[side] = {"database": db, "error": str(exc).splitlines()[0]}
            continue
        by_rule: dict[str, int] = {}
        for f in found:
            by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
        out[side] = {"database": db, "findings": len(found), "by_rule": by_rule,
                     "items": [str(f) for f in found], "skipped": reason}
    return out


def errors(result: dict) -> list[str]:
    """Unexpected errors (an `expect_error` step that failed as predicted is not one)."""
    bad = []
    for q in result["questions"]:
        for r in [*q.get("legacy_steps", [q["legacy"]]), q["modern"]]:
            if r.get("error"):
                bad.append(f"{q['id']} {r.get('file')}: {r['error']}")
    return bad


def main(out: Path | None = None) -> int:
    result = run()
    path = out or BENCHMARK_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(result, indent=2, default=str)
    path.write_text(text, encoding="utf-8")
    if out is None:  # keep a copy outside site/ (a report build wipes site/) and refresh the docs
        STATE_JSON.parent.mkdir(parents=True, exist_ok=True)
        STATE_JSON.write_text(text, encoding="utf-8")
        from legacy import report

        for p in report.write(result):
            print(f"wrote {p}")
    for q in result["questions"]:
        leg, mod = q["legacy"], q["modern"]
        print(f"{q['id']} {q['observed_failure']:<10} legacy={leg.get('answer')!s:<14} {leg.get('ms')!s:>9} ms  "
              f"modern={mod.get('answer')!s:<14} {mod.get('ms')!s:>9} ms  {leg.get('note', '')}")
    bad = errors(result)
    for b in bad:
        print("error:", b)
    print(f"benchmark: {len(result['questions'])} question(s) run, {len(result['skipped'])} skipped -> {path}")
    return 1 if bad else 0


def copy_to_site(site_dir: Path, result_path: Path | None = None) -> Path | None:
    """Put the last measured JSON into a (re)built site at ``data/legacy_benchmark.json``."""
    src = result_path or STATE_JSON
    if not src.exists():
        return None
    dst = site_dir / "data" / "legacy_benchmark.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return dst
