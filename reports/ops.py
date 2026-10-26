"""Operations page (``ops.html``): is today's data on time, gated and reconciled?

A report plug-in like any area package: ``build(con, out_dir) -> list[Page]``. It reads only the
``audit`` schema (pipeline runs, diagnostics, reconciliation) plus the dealer packets already
copied into the site, and checks the service levels in docs/operating-model/SLA.md:

- **SLA**: reports for business day D are published by 07:00 local time on D. A day is met when a
  run whose ``reports`` step succeeded finished inside the window (07:00 on D-1, 07:00 on D].
- Every diagnostics check links to its runbook entry in docs/operating-model/RUNBOOK.md.
- **Database health** (V6 edit 20, ``audit.db_health`` from pipeline/db_health.py): empty serving tables,
  Postgres statistics not refreshed, and the refresh login used for ad-hoc queries. Postgres rows say
  ``skipped`` when the server was down.
- **Semantic model vs warehouse** (V7 edit 33, semantic/counts.py): six headline numbers worked out from the
  Power BI model's own measures and from the certified marts, plus when the model files and the warehouse
  were last built.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd

from reports.build import Kpi, Page, Section, num, pct

NAV_LABEL = "Operations"
SLA_TZ: tzinfo
try:  # the business's local time zone; override with SLA_TIMEZONE
    SLA_TZ = ZoneInfo(os.environ.get("SLA_TIMEZONE", "America/Chicago"))
except ZoneInfoNotFoundError:  # no tz database on this machine
    SLA_TZ = UTC
SLA_DEADLINE = time(7, 0)
SLA_TARGET = 0.95  # SLO: share of business days met over the trailing 20 business days
SLA_WINDOW_DAYS = 20
RUNBOOK = "docs/operating-model/RUNBOOK.md"


def _has(con, schema: str, table: str) -> bool:
    return bool(con.execute(
        "select count(*) from information_schema.tables where table_schema = ? and table_name = ?", [schema, table]
    ).fetchone()[0])


def published_runs(runs: pd.DataFrame) -> pd.DataFrame:
    """Runs whose reports step succeeded, with the local finish time. Input: audit.pipeline_run rows (UTC)."""
    if runs.empty:
        return runs.assign(finished_local=pd.Series(dtype="object"))

    def reports_ok(steps: str) -> bool:
        try:
            return any(s.get("name") == "reports" and s.get("status") == "ok" for s in json.loads(steps or "[]"))
        except (TypeError, ValueError):
            return False

    ok = runs[(runs["status"] == "ok") & runs["steps"].map(reports_ok)].copy()
    ok["finished_local"] = [pd.Timestamp(t).tz_localize("UTC").tz_convert(SLA_TZ).to_pydatetime()
                            for t in ok["finished_at"]]
    return ok


def next_business_day(d: date) -> date:
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def due_day(now: datetime) -> date:
    """The business day whose 07:00 deadline is the next one at or after ``now``."""
    d = now.date() if now.time() <= SLA_DEADLINE else now.date() + timedelta(days=1)
    return next_business_day(d)


def business_days(end: date, n: int) -> list[date]:
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days[::-1]


def sla_days(finished_local: list[datetime], days: list[date], now: datetime) -> pd.DataFrame:
    """One row per business day.

    met     a published run finished in (07:00 on D-1, 07:00 on D] local time
    missed  the deadline passed with no such run
    pending the deadline is still ahead and nothing has published yet
    untracked  the deadline came before the first recorded run (excluded from the SLO rate)
    """
    first = min(finished_local) if finished_local else None
    rows = []
    for d in days:
        deadline = datetime.combine(d, SLA_DEADLINE, tzinfo=SLA_TZ)
        hits = [t for t in finished_local if deadline - timedelta(days=1) < t <= deadline]
        if hits:
            status = "met"
        elif deadline > now:
            status = "pending"
        elif first is None or deadline < first:
            status = "untracked"
        else:
            status = "missed"
        rows.append({"business_day": d, "status": status, "published_at": max(hits) if hits else None})
    return pd.DataFrame(rows, columns=["business_day", "status", "published_at"])


def slo_rate(sla: pd.DataFrame) -> float | None:
    """Share of tracked, closed business days that were met (pending and untracked days excluded)."""
    closed = sla[sla["status"].isin(["met", "missed"])]
    return float((closed["status"] == "met").mean()) if len(closed) else None


def publishing_now(run) -> bool:
    """True when ``run`` is the provisional 'running' row of the pipeline run that is rendering this
    site right now (pipeline.run sets PIPELINE_RUN_ID and writes that row just before the reports step,
    which is the last step). The site only exists once this step renders it, so the run's publish time
    is the render time. A 'running' row of any other run id is a run that stopped before finishing."""
    current = os.environ.get("PIPELINE_RUN_ID", "")
    return run is not None and run.get("status") == "running" and bool(current) and run.get("run_id") == current


def last_run_kpi(run, now: datetime) -> Kpi:
    """The 'Last pipeline run' tile, shared by ops.html and quality.html. ``run`` is a mapping with
    status, run_id and finished_at (UTC) or None."""
    if run is None:
        return Kpi("Last pipeline run", "NONE", "no run recorded", "critical")
    if publishing_now(run):
        return Kpi("Last pipeline run", "OK",
                   f"published {now.astimezone(SLA_TZ):%H:%M} local, all gates passed", "good")
    if run.get("status") == "running":
        return Kpi("Last pipeline run", "INCOMPLETE", f"run {run.get('run_id', '')} stopped before it finished",
                   "critical")
    fin = run.get("finished_at")
    sub = f"{pd.Timestamp(fin):%b %d %H:%M} UTC" if fin is not None and str(fin) else ""
    return Kpi("Last pipeline run", str(run.get("status", "none")).upper(), sub,
               "good" if run.get("status") == "ok" else "critical")


RECON_WAIVERS = ("not_migrated",)  # reconciliation statuses that are documented, not breaks


def _status(ok: bool | None, text: str) -> dict:
    return {"status": "" if ok is None else ("good" if ok else "critical"), "text": text}


ENV_DIFF_PATH = Path(os.environ.get("ENV_DIFF_PATH", Path(__file__).resolve().parents[1] / "data" / "env" /
                                   "env_diff.json"))


def env_diff_section(path: Path | None = None) -> Section | None:
    """Latest UAT-vs-prod promotion check from tools/env_diff.py (docs/operating-model/RELEASE.md), if any."""
    try:
        d = json.loads((path or ENV_DIFF_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    c = d.get("counts", {})
    base, cand = str(d.get("base", "base")).upper(), str(d.get("candidate", "candidate")).upper()
    caption = (f"{cand} vs {base} at {d.get('generated_at', '')}: "
               f"{d.get('tables', 0)} mart tables, {c.get('pass', 0)} totals equal, {c.get('explained', 0)} explained, "
               f"{c.get('fail', 0)} unexplained. Promotion needs 0 unexplained (python tools/env_diff.py).")
    rows = [[r["table"], r["measure"], "" if r["base"] is None else f"{r['base']:,.2f}",
             "" if r["candidate"] is None else f"{r['candidate']:,.2f}",
             "" if r["diff"] is None else f"{r['diff']:,.2f}",
             _status(None if r["status"] == "explained" else False, r["status"]), r.get("reason", "")]
            for r in d.get("differences", [])]
    if not rows:
        rows = [["all mart tables", "rows and money totals", "", "", "0.00", _status(True, "pass"), ""]]
    return Section(f"Promotion check: {cand} vs {base}", caption,
                   cols=["Table", "Measure", base, cand, "Diff", "Status", "Reason"],
                   rows=rows, table_open=True, wide=True)


def semantic_counts_section(con) -> Section | None:
    """Headline counts from the semantic model's measures vs the certified marts (semantic/counts.py)."""
    from semantic import counts

    if not counts.load_models():
        return None
    try:
        results = counts.check(con)
    except Exception:  # noqa: BLE001 - an optional Operations section never breaks the page
        return None
    agree = sum(r.ok for r in results)

    def when(ts) -> str:
        return f"{ts.astimezone(SLA_TZ):%b %d %H:%M}" if ts else "unknown"

    def show(r, v) -> str:
        return "" if v is None else (f"{v:,.2f}" if r.headline.kind == "money" else f"{v:,.0f}")

    caption = (f"{agree} of {len(results)} agree. Each number is worked out twice: once from the Power BI model's "
               "own measure, over the table it loads, and once from the mart the report pages use. A gap means "
               "the model points at the wrong table or column. Model files written "
               f"{when(counts.model_written_at())}; warehouse built {when(counts.warehouse_built_at(con))} "
               f"({SLA_TZ}). Run it yourself: python -m semantic count-check.")
    rows = [[r.headline.label, f"{r.headline.model}: {r.headline.measure}", show(r, r.model_value),
             show(r, r.warehouse_value), r.headline.warehouse_source,
             _status(r.ok, "agree" if r.ok else (r.note or "differs"))] for r in results]
    return Section("Power BI model vs warehouse", caption,
                   cols=["Number", "Model measure", "Model", "Warehouse", "Warehouse source", "Status"],
                   rows=rows, table_open=agree < len(results), wide=True)


HEALTH_LABELS = {"zero_row_table": "Empty serving table", "stale_statistics": "Statistics not refreshed",
                 "refresh_login_adhoc": "Refresh login used ad hoc"}


def db_health_section(con) -> Section | None:
    """Latest run's database-health findings (audit.db_health), problems first; None before the first run."""
    if not _has(con, "audit", "db_health"):
        return None
    df = con.execute("""
        select * from audit.db_health where run_id = (select max(run_id) from audit.db_health)
        order by case status when 'warn' then 0 when 'skipped' then 1 else 2 end, check_name, object_name
    """).df()
    if df.empty:
        return None
    warns = int((df["status"] == "warn").sum())
    rows = [[HEALTH_LABELS.get(r.check_name, r.check_name), r.engine, r.object_name,
             _status({"pass": True, "warn": False}.get(r.status), r.status),
             "" if pd.isna(r.measured_value) else f"{r.measured_value:,.4g}", r.detail]
            for r in df.itertuples()]
    return Section("Database health",
                   f"{warns} finding(s) in the latest run. Empty tables in the serving schemas, Postgres tables whose "
                   "statistics are missing or stale, and sessions under the BI refresh login from a client that "
                   "is not a scheduled refresh. Informational: these never block publishing.",
                   cols=["Check", "Engine", "Object", "Status", "Measured", "Detail"],
                   rows=rows, table_open=bool(warns), wide=True)


def build(con, out_dir: Path, now: datetime | None = None) -> list[Page]:
    if not _has(con, "audit", "pipeline_run"):
        return []
    runs = con.execute("select * from audit.pipeline_run order by finished_at").df()
    pub = published_runs(runs)
    now = (now or datetime.now(SLA_TZ)).astimezone(SLA_TZ)
    last = runs.iloc[-1].to_dict() if len(runs) else None
    published = list(pub["finished_local"])
    if publishing_now(last):  # this run publishes as this page renders: count it at the render time
        published.append(now)
    sla = sla_days(published, business_days(due_day(now), SLA_WINDOW_DAYS), now)
    met_rate = slo_rate(sla)

    diag = pd.DataFrame()
    if _has(con, "audit", "diagnostics_result"):
        diag = con.execute("""
            select * from audit.diagnostics_result
            where run_id = (select max(run_id) from audit.diagnostics_result) order by check_name
        """).df()
    recon = pd.DataFrame()
    if _has(con, "audit", "reconciliation"):
        recon = con.execute("""
            select * from audit.reconciliation
            where run_id = (select max(run_id) from audit.reconciliation)
            order by status <> 'pass' desc, source_system, entity, comparison
        """).df()
    # A documented waiver (pipeline.reconcile Break.waiver, e.g. not_migrated) is not a break; anything else is.
    breaks = int((~recon["status"].isin(["pass", *RECON_WAIVERS])).sum()) if len(recon) else 0
    diag_ok = int((diag["status"] == "pass").sum()) if len(diag) else 0

    kpis = [
        last_run_kpi(last, now),
        Kpi("Reports by 7 AM (SLO)", pct(met_rate, 0) if met_rate is not None else "n/a",
            f"tracked days of the last {SLA_WINDOW_DAYS}, target {SLA_TARGET:.0%}",
            "" if met_rate is None else ("good" if met_rate >= SLA_TARGET else "warning")),
        Kpi("Diagnostics passing", f"{diag_ok} / {len(diag)}" if len(diag) else "n/a", "gate: blocks publish",
            "" if not len(diag) else ("good" if diag_ok == len(diag) else "critical")),
        Kpi("Reconciliation breaks", num(breaks) if len(recon) else "n/a", "source -> raw -> marts, $0.01 / 0 rows",
            "" if not len(recon) else ("good" if breaks == 0 else "critical")),
    ]
    sections = [Section(
        "Service level: reports published by 7 AM",
        f"A business day is met when a run that published the reports finished in the 24 hours before 07:00 "
        f"({SLA_TZ}) that day. Days before the first recorded run are untracked and left out of the SLO. "
        "In plain words: a run counts toward the next morning's 7 AM, so a publish time from the afternoon "
        "before is normal.",
        cols=["Business day", "Status", "Published at (local)"],
        rows=[[f"{r.business_day:%a %b %d}",
               _status({"met": True, "missed": False}.get(r.status), r.status.title()),
               f"{r.published_at:%b %d %H:%M}" if pd.notna(r.published_at) else ""]
              for r in sla.iloc[::-1].itertuples()],
        table_open=True)]
    if len(diag):
        sections.append(Section(
            "Diagnostics gate (latest run)",
            f"The checks that stop publishing when they fail. Each has a runbook entry in {RUNBOOK}.",
            cols=["Check", "Status", "Measured", "Threshold", "Runbook"],
            rows=[[r.check_name, _status(r.status == "pass", r.status),
                   "" if pd.isna(r.measured_value) else f"{r.measured_value:,.4g}",
                   "" if pd.isna(r.threshold) else f"{r.threshold:,.4g}", f"RUNBOOK.md#{r.check_name}"]
                  for r in diag.itertuples()],
            table_open=True, wide=True))
    if len(recon):
        sections.append(Section(
            "Reconciliation (latest run)", "Row counts and dollars tied between the source systems, raw and marts.",
            cols=["Source", "Entity", "Measure", "Comparison", "Row diff", "Amount diff", "Status"],
            rows=[[r.source_system, r.entity, r.measure, r.comparison, num(r.row_diff),
                   "" if pd.isna(r.amount_diff) else f"{r.amount_diff:,.2f}",
                   _status(None if r.status in RECON_WAIVERS else r.status == "pass", r.status)]
                  for r in recon.itertuples()],
            table_open=True, wide=True))
    model_counts = semantic_counts_section(con)
    if model_counts is not None:
        sections.append(model_counts)
    packets = sorted(out_dir.glob("dealers/*/*/index.html"))
    if packets:
        sections.append(Section(
            "Dealer packets", f"{len(packets)} per-dealer packets from python -m publish, each filtered to one "
            "dealer, in the F&IP theme with a thin stripe in its program's colour.",
            cols=["Administrator", "Dealer", "Packet"],
            rows=[[p.parent.parent.name, p.parent.name,
                   {"text": "Open packet", "href": p.relative_to(out_dir).as_posix()}] for p in packets],
            table_open=True))
    health = db_health_section(con)
    if health is not None:
        sections.append(health)
    env_diff = env_diff_section()
    if env_diff is not None:
        sections.append(env_diff)
    return [Page("ops.html", "Operations", "Is today's data on time, gated and reconciled? Service levels, quality "
                 "gates and the dealer packets shipped by the last run.", kpis, sections)]
