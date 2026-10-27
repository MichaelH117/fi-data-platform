"""'Results at a glance': proof tiles for the Start here page and the Executive overview.

Every number is read at build time from the artifacts of one completed full pipeline run, never typed in:

- ``data/runs/<run_id>.json``: the run manifest (status, wall-clock seconds, steps);
- ``data/runs/<run_id>/dbt_build.json``: that run's dbt build results (data tests, unit tests, models);
- ``dbt/target/run_results.json``: the reconciliation gate's ``dbt test``, used only when it ran inside that run;
- ``audit.layer_row_count`` / ``audit.diagnostics_result`` / ``audit.reconciliation`` rows for that run_id;
- ``data/runs/_pytest.json``: the last ``python tools/record_pytest.py`` result;
- the legacy benchmark JSON the Legacy vs better page reads (``legacy.report.load``);
- the dealer packets copied into the site.

The same dict is written to ``docs/showcase_stats.json`` for the README. Numbers are never rounded up:
compact figures are truncated (14,954,790 -> 14.9M) and the exact value is printed underneath.

What each headline measures (the tile labels say the same):

- rows: rows *stored* in the DuckDB warehouse's tables and snapshots, all layers, for that run. Views (all of
  staging, a few intermediate and mart models) are counted separately and left out of the headline: their rows
  are computed from stored ones, so adding them would count the same data twice. The opt-in PostgreSQL sync
  copies a subset of the tables and is not this number.
- dbt: every node of the pipeline's ``dbt build`` (models, snapshots, seeds, data tests, unit tests). A
  ``warn`` is a data-quality test with ``severity: warn``: it reports rows without stopping the build.
- legacy speed: the benchmark question whose observed failure is "slow" (both sides finished, so the two times
  compare like for like). A legacy query that hit its timeout is reported as "timed out at <budget>", never as
  a speed.
"""

from __future__ import annotations

import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path

from reports.build import Kpi

RUNS_ENV = "PIPELINE_RUNS_DIR"
PYTEST_JSON = "_pytest.json"
GATES_JSON = "_gates.json"
RECON_WAIVERS = ("not_migrated",)  # same rule as reports/ops.py: a documented waiver is not a break
ARROW = "→"


# --------------------------------------------------------------------------- formatting (never rounds up)
def trunc(v: float, digits: int = 1) -> float:
    f = 10 ** digits
    return math.floor(v * f) / f


def compact(n: int | float) -> str:
    """24,024,818 -> '24.0M'; 13,999 -> '13.9K'. Truncated, so the label is never above the real number."""
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(n) >= size:
            return f"{trunc(n / size):,.1f}{suffix}"
    return f"{int(n):,}"


def duration(seconds: float) -> str:
    s = int(seconds)  # whole seconds, truncated
    return f"{s // 60} min {s % 60:02d} s" if s >= 60 else f"{s} s"


def ms(v: float) -> str:
    return f"{trunc(v):,.1f} ms"


def budget(b: str | None) -> str:
    """A benchmark budget as written in the run ('60s') -> '60 s'."""
    return "its budget" if not b else (f"{b[:-1]} s" if b.endswith("s") and b[:-1].isdigit() else b)


# --------------------------------------------------------------------------- reading the artifacts
def runs_dir(root: Path) -> Path:
    return Path(os.environ.get(RUNS_ENV, root / "data" / "runs"))


def _json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def latest_full_run(runs: Path) -> dict | None:
    """Newest finished run that ran every step (no --only / --start) and finished ok."""
    for path in sorted(runs.glob("*.json"), reverse=True) if runs.is_dir() else []:
        if path.name.startswith("_"):
            continue
        run = _json(path)
        args = (run or {}).get("args") or {}
        if run and run.get("status") == "ok" and not args.get("only") and not args.get("start"):
            return run
    return None


def _dbt(runs: Path, root: Path, run: dict) -> dict | None:
    build = _json(runs / run["run_id"] / "dbt_build.json")
    if not build:
        return None
    counts: dict[str, dict[str, int]] = {}
    for r in build.get("results", []):
        kind = r["unique_id"].split(".")[0]
        counts.setdefault(kind, {}).setdefault(r["status"], 0)
        counts[kind][r["status"]] += 1
    tests = counts.get("test", {})
    nodes = list(build.get("results", []))
    warns = [{"test": r["unique_id"].split(".")[-1], "rows": r.get("failures")}
             for r in nodes if r["status"] == "warn"]
    out = {
        "build_nodes_total": len(nodes),
        "build_nodes_ok": sum(1 for r in nodes if r["status"] in ("success", "pass")),
        "build_nodes_warned": len(warns),
        "build_nodes_failed": sum(1 for r in nodes if r["status"] in ("error", "fail")),
        "build_nodes_skipped": sum(1 for r in nodes if r["status"] == "skipped"),
        "warnings": warns,
        "snapshots_built": counts.get("snapshot", {}).get("success", 0),
        "seeds_loaded": counts.get("seed", {}).get("success", 0),
        "data_tests_passed": tests.get("pass", 0),
        "data_tests_warned": tests.get("warn", 0),
        "data_tests_failed": tests.get("fail", 0) + tests.get("error", 0),
        "data_tests_total": sum(tests.values()),
        "unit_tests_passed": counts.get("unit_test", {}).get("pass", 0),
        "models_built": counts.get("model", {}).get("success", 0),
        "build_seconds": round(float(build.get("elapsed_time", 0.0)), 2),
        "source": f"data/runs/{run['run_id']}/dbt_build.json",
        "gate_tests_passed": None,
    }
    # The reconciliation gate runs `dbt test --select tag:reconciliation` after the build; its results are
    # counted only when that file was written inside this run's window (a later ad-hoc test overwrites it).
    gate = _json(root / "dbt" / "target" / "run_results.json")
    if gate and run.get("started_at") and run.get("finished_at"):
        at = gate["metadata"]["generated_at"][:19]
        if run["started_at"][:19] <= at <= run["finished_at"][:19] and \
                not any(r["unique_id"].startswith("model.") for r in gate.get("results", [])):
            out["gate_tests_passed"] = sum(1 for r in gate["results"] if r["status"] == "pass")
            out["gate_tests_total"] = len(gate["results"])
    return out


def _audit(con, run_id: str) -> dict:
    """Warehouse rows and gate results the diagnostics and reconcile steps wrote for ``run_id``."""
    out: dict = {"warehouse": None, "diagnostics": None, "reconciliation": None}
    if con is None or not hasattr(con, "execute"):
        return out
    try:
        layers = con.execute(
            "select layer, coalesce(materialized = 'view', false), count(*), sum(row_count) "
            "from audit.layer_row_count where run_id = ? group by 1, 2", [run_id]).fetchall()
        if layers:
            by: dict[str, dict[str, int]] = {}
            for lyr, is_view, n, rows in layers:
                d = by.setdefault(str(lyr), {"relations": 0, "rows": 0, "view_relations": 0, "view_rows": 0})
                d["view_relations" if is_view else "relations"] += int(n)
                d["view_rows" if is_view else "rows"] += int(rows or 0)
            out["warehouse"] = {
                "engine": "DuckDB", "measures": "rows stored in tables and snapshots, all layers; views excluded",
                "rows": sum(v["rows"] for v in by.values()),
                "relations": sum(v["relations"] for v in by.values()),
                "view_rows": sum(v["view_rows"] for v in by.values()),
                "view_relations": sum(v["view_relations"] for v in by.values()),
                "by_layer": by, "source": f"audit.layer_row_count, run {run_id}"}
        diag = con.execute("select status, count(*) from audit.diagnostics_result where run_id = ? group by 1",
                           [run_id]).fetchall()
        if diag:
            d = dict(diag)
            out["diagnostics"] = {"passed": int(d.get("pass", 0)), "total": int(sum(d.values())),
                                  "source": f"audit.diagnostics_result, run {run_id}"}
        rec = con.execute("select status, count(*) from audit.reconciliation where run_id = ? group by 1",
                          [run_id]).fetchall()
        if rec:
            r = dict(rec)
            waived = int(sum(v for k, v in r.items() if k in RECON_WAIVERS))
            out["reconciliation"] = {"passed": int(r.get("pass", 0)), "waived": waived,
                                     "breaks": int(sum(r.values())) - int(r.get("pass", 0)) - waived,
                                     "total": int(sum(r.values())),
                                     "source": f"audit.reconciliation, run {run_id}"}
    except Exception:  # noqa: BLE001 - no audit schema (a fresh or test warehouse): the tile says n/a
        pass
    return out


def _legacy() -> dict | None:
    try:
        from legacy import report
    except ImportError:
        return None
    data = report.load()
    if not data or not data.get("questions"):
        return None
    qs = data["questions"]
    wrong = [q["id"] for q in qs if q["observed_failure"] not in ("none", "error")]
    modern_bad = [q["id"] for q in qs if q["modern"].get("error") or q["modern"].get("timed_out")]
    def steps(q: dict) -> list[dict]:
        return q.get("legacy_steps") or [q["legacy"]]

    def timed_out(q: dict) -> bool:
        return any(s.get("timed_out") for s in steps(q))

    finished = [q for q in qs if not timed_out(q) and q["legacy"].get("ms") and q["modern"].get("ms")]
    slow = next((q for q in finished if q["observed_failure"] == "slow"), None) or \
        max(finished, key=lambda q: q["legacy"]["ms"], default=None)
    timeouts = [{"id": q["id"], "question": q["question"],
                 "budget": next(s.get("timeout") for s in steps(q) if s.get("timed_out")),
                 "modern_ms": q["modern"].get("ms")} for q in qs if timed_out(q)]
    src = report.STATE_JSON if report.STATE_JSON.exists() else report.SNAPSHOT
    return {
        "questions": len(qs), "legacy_wrong_or_failed": len(wrong), "modern_errors_or_timeouts": len(modern_bad),
        "slow_query": {"id": slow["id"], "question": slow["question"], "observed_failure": slow["observed_failure"],
                       "legacy_ms": slow["legacy"]["ms"], "modern_ms": slow["modern"]["ms"],
                       "legacy_budget": slow["legacy"].get("timeout")} if slow else None,
        "legacy_timeouts": timeouts,
        "measured_at": data.get("generated_at"),
        "source": src.relative_to(report.ROOT).as_posix() if src.is_relative_to(report.ROOT) else src.name,
    }


def collect(root: Path, con=None, packets: int | None = None) -> dict:
    """All proof numbers, with where each one came from. Missing artifacts give None, never a guess."""
    runs = runs_dir(root)
    run = latest_full_run(runs)
    stats: dict = {"run_id": None, "rebuild": None, "dbt": None, "warehouse": None, "diagnostics": None,
                   "reconciliation": None, "gates": None, "pytest": None, "legacy": _legacy(),
                   "dealer_packets": packets}
    if run:
        stats["run_id"] = run["run_id"]
        stats["rebuild"] = {"seconds": run["seconds"], "steps": len(run.get("steps", [])),
                            "steps_ok": sum(1 for s in run.get("steps", []) if s.get("status") == "ok"),
                            "started_at": run.get("started_at"), "finished_at": run.get("finished_at"),
                            "source": f"data/runs/{run['run_id']}.json"}
        stats["dbt"] = _dbt(runs, root, run)
        stats.update(_audit(con, run["run_id"]))
        gates = _json(runs / GATES_JSON) or {}
        mine = {k: v.get("status") for k, v in gates.items()
                if isinstance(v, dict) and v.get("run_id") == run["run_id"]}
        if mine:
            stats["gates"] = {"ok": sum(1 for v in mine.values() if v == "ok"), "total": len(mine),
                              "names": sorted(mine), "source": f"data/runs/{GATES_JSON}"}
    pt = _json(runs / PYTEST_JSON)
    if pt and "passed" in pt:
        stats["pytest"] = {k: pt.get(k) for k in ("passed", "failed", "skipped", "errors", "deselected",
                                                  "collected", "selection", "seconds", "recorded_at")}
        stats["pytest"]["source"] = f"data/runs/{PYTEST_JSON}"
    return stats


# --------------------------------------------------------------------------- tiles
def tiles(s: dict) -> list[Kpi]:
    """One tile per proof, in reading order. Labels are literal so the explanation map can find them."""
    na = "n/a"
    out = []

    w = s.get("warehouse")
    if w:
        marts = (w["by_layer"].get("marts") or {}).get("rows", 0)
        sub = (f"{w['rows']:,} rows in {w['relations']:,} {w['engine']} tables and snapshots, all layers "
               f"({marts:,} in the marts reports read); {w['view_relations']:,} views not counted")
    out.append(Kpi("Rows stored in the warehouse", compact(w["rows"]) if w else na,
                   sub if w else "no row-count audit for the latest full run", "", metric_id="proof_warehouse_rows"))

    t = s.get("dbt")
    if t:
        warn = "; ".join(x["test"] + (f", {x['rows']:,} rows" if x.get("rows") is not None else "")
                         for x in t["warnings"])
        sub = (f"pipeline build: {t['models_built']} models, {t['snapshots_built']} snapshots, "
               f"{t['seeds_loaded']} seeds, {t['data_tests_total']} data tests, {t['unit_tests_passed']} unit tests"
               f"; {t['build_nodes_failed']} failed"
               + (f"; {t['build_nodes_warned']} known data-quality warning ({warn})" if t["build_nodes_warned"]
                  else "")
               + (f"; {t['gate_tests_passed']} of {t['gate_tests_total']} gate tests after"
                  if t.get("gate_tests_passed") is not None else ""))
        status = "good" if t["build_nodes_failed"] == 0 else "critical"
    out.append(Kpi("dbt build nodes passed", f"{t['build_nodes_ok']:,} of {t['build_nodes_total']:,}" if t else na,
                   sub if t else "no saved dbt build for the latest full run", status if t else "",
                   metric_id="proof_dbt_tests"))

    p = s.get("pytest")
    if p:
        sub = (f"{p['failed'] or 0} failed, {p['skipped'] or 0} skipped of {p['collected']:,} run"
               + (f"; {p['deselected']} slow end-to-end tests not in this record" if p.get("deselected") else "")
               + f"; recorded {str(p['recorded_at'])[:10]}")
        status = "good" if not p["failed"] and not p.get("errors") else "critical"
    out.append(Kpi("Python tests passed", f"{p['passed']:,}" if p else na,
                   sub if p else "run python tools/record_pytest.py to record a count", status if p else "",
                   metric_id="proof_pytest"))

    r = s.get("rebuild")
    out.append(Kpi("Full rebuild time", duration(r["seconds"]) if r else na,
                   f"{r['steps_ok']} of {r['steps']} steps ok, source data to published site, one command "
                   f"({r['seconds']:,.2f} s)" if r else "no completed full run recorded",
                   "good" if r and r["steps_ok"] == r["steps"] else "", metric_id="proof_rebuild_time"))

    dg, rc = s.get("diagnostics"), s.get("reconciliation")
    if dg and rc:
        sub = (f"{dg['passed']} of {dg['total']} diagnostics passed; {rc['passed']} of {rc['total']} "
               f"reconciliation checks tied out" + (f", {rc['waived']} documented waiver" if rc["waived"] else "")
               + (f"; {s['gates']['ok']} of {s['gates']['total']} publish gates ok" if s.get("gates") else ""))
        status = "good" if rc["breaks"] == 0 and dg["passed"] == dg["total"] else "critical"
    out.append(Kpi("Gate breaks", f"{rc['breaks']:,}" if dg and rc else na,
                   sub if dg and rc else "no gate results for the latest full run", status if dg and rc else "",
                   metric_id="proof_gates"))

    lg = s.get("legacy")
    out.append(Kpi("Legacy answers wrong", f"{lg['legacy_wrong_or_failed']}/{lg['questions']} {ARROW} "
                   f"{lg['modern_errors_or_timeouts']}" if lg else na,
                   f"old warehouse wrong or failed on {lg['legacy_wrong_or_failed']} of {lg['questions']} business "
                   f"questions; new platform: {lg['modern_errors_or_timeouts']} errors or timeouts"
                   if lg else "no legacy benchmark measured", "good" if lg and not lg["modern_errors_or_timeouts"]
                   else "", metric_id="proof_legacy_wrong"))

    sl = (lg or {}).get("slow_query")
    if sl:
        sub = (f"{sl['id'].upper()} ({sl['question']}), the benchmark's slow query, legacy vs new, same server "
               "and rows" + "".join(f"; legacy {x['id'].upper()} timed out at {budget(x['budget'])} (new: "
                                    f"{ms(x['modern_ms'])})" for x in lg["legacy_timeouts"]))
    out.append(Kpi("Slow legacy query", f"{ms(sl['legacy_ms'])} {ARROW} {ms(sl['modern_ms'])}" if sl else na,
                   sub if sl else "no legacy benchmark measured", "", metric_id="proof_legacy_speed"))

    n = s.get("dealer_packets")
    out.append(Kpi("Dealer packets", f"{n:,}" if n else na,
                   "one packet per reportable dealer, filtered to that dealer's rows only" if n
                   else "publish has not produced packets", "", metric_id="proof_dealer_packets"))
    return out


def basis_line(s: dict) -> str:
    """One sentence under the strip: which run and when the benchmark was measured."""
    parts = [f"pipeline run {s['run_id']}" if s.get("run_id") else "no completed full run"]
    if s.get("legacy"):
        parts.append(f"legacy benchmark measured {s['legacy']['measured_at']}")
    if s.get("pytest"):
        parts.append(f"tests recorded {str(s['pytest']['recorded_at'])[:16].replace('T', ' ')}")
    return "Computed when this site was built, from " + "; ".join(parts) + ". Synthetic data."


def tile_values(ks: list[Kpi]) -> dict[str, str]:
    return {k.metric_id: k.value for k in ks}


def write_json(s: dict, path: Path) -> Path:
    payload = {"generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "note": "Written by python -m reports (reports/showcase.py). Every value comes from the run "
                       "artifacts named in its 'source'; compact tile labels are truncated, never rounded up.",
               "stats": s, "tiles": {k.metric_id: {"label": k.label, "value": k.value, "sub": k.sub}
                                     for k in tiles(s)}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path
