"""Reporting Manager (ops): operating-model docs, the report plug-in hook, the Operations page and
the generated data dictionary. Unit tests only: nothing here needs a built warehouse."""

from __future__ import annotations

import json
import re
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

import reports.build as rb
from reports import dictionary, ops
from reports.__main__ import broken_links

ROOT = Path(__file__).resolve().parents[1]
OPS_DOCS = ROOT / "docs" / "operating-model"
TZ = ops.SLA_TZ


# --------------------------------------------------------------------------- docs
@pytest.mark.parametrize("name", ["README.md", "SLA.md", "RUNBOOK.md", "INCIDENT_TEMPLATE.md", "INTAKE_FORM.md",
                                  "DATA_DICTIONARY.md", "RELEASE.md"])
def test_operating_model_doc_exists(name):
    text = (OPS_DOCS / name).read_text(encoding="utf-8")
    assert text.startswith("# ") and len(text) > 500


def test_example_postmortem_follows_template():
    pms = list((OPS_DOCS / "postmortems").glob("*.md"))
    assert pms, "one example postmortem is required"
    text = pms[0].read_text(encoding="utf-8")
    template_headings = re.findall(r"^### (.+)$", (OPS_DOCS / "INCIDENT_TEMPLATE.md").read_text(encoding="utf-8"),
                                   re.M)
    for h in ("Summary", "Impact", "Timeline", "Detection", "Root cause", "Action items", "Verification"):
        assert any(t.startswith(h) for t in template_headings)
        assert re.search(rf"^## {h}", text, re.M), f"postmortem missing section {h}"
    assert "diag_product_dim_match_rate" in text


def _diagnostic_checks() -> set[str]:
    return {p.stem for p in (ROOT / "dbt" / "tests" / "diagnostics").glob("diag_*.sql")}


def test_runbook_has_an_entry_for_every_diagnostic():
    from pipeline.diagnostics import MEASURES

    checks = _diagnostic_checks() | set(MEASURES)
    assert len(checks) >= 8
    headings = set(re.findall(r"^### `([a-z_]+)`", (OPS_DOCS / "RUNBOOK.md").read_text(encoding="utf-8"), re.M))
    assert checks <= headings, f"runbook missing: {sorted(checks - headings)}"
    assert "reconcil" in (OPS_DOCS / "RUNBOOK.md").read_text(encoding="utf-8").lower()


def test_sla_doc_matches_the_ops_page_deadline():
    text = (OPS_DOCS / "SLA.md").read_text(encoding="utf-8")
    assert f"{ops.SLA_DEADLINE:%H:%M}" in text and f"{ops.SLA_TARGET:.0%}" in text
    assert "07:00" in text


def test_operating_model_links_resolve():
    for page in OPS_DOCS.rglob("*.md"):
        for target in re.findall(r"\]\(([^)#]+\.md)(?:#[^)]*)?\)", page.read_text(encoding="utf-8")):
            if target.startswith("http"):
                continue
            assert (page.parent / target).resolve().is_file(), f"{page.name} -> {target}"


# --------------------------------------------------------------------------- SLA logic
def _local(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=TZ)


def test_sla_days_met_missed_pending_untracked():
    mon, tue, wed, thu = date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21), date(2026, 10, 22)
    runs = [_local(tue, 6, 30),          # before Tue 07:00 -> Tue met
            _local(wed, 7, 30)]          # after Wed 07:00 -> counts for Thu, Wed missed
    now = _local(thu, 6, 0)              # Thu deadline still ahead, but already published -> met
    got = ops.sla_days(runs, [mon, tue, wed, thu], now).set_index("business_day")["status"].to_dict()
    assert got == {mon: "untracked", tue: "met", wed: "missed", thu: "met"}
    nothing_yet = ops.sla_days([_local(tue, 6, 30)], [tue, thu], now)["status"].tolist()
    assert nothing_yet == ["met", "pending"]


def test_slo_rate_ignores_pending_and_untracked():
    sla = pd.DataFrame({"status": ["untracked", "met", "missed", "met", "pending"]})
    assert ops.slo_rate(sla) == pytest.approx(2 / 3)
    assert ops.slo_rate(pd.DataFrame({"status": ["untracked", "pending"]})) is None


def test_due_day_rolls_past_deadline_and_weekend():
    fri = date(2026, 10, 23)
    assert ops.due_day(_local(fri, 6, 59)) == fri
    assert ops.due_day(_local(fri, 7, 1)) == date(2026, 10, 26)  # Monday
    assert ops.business_days(date(2026, 10, 26), 3) == [date(2026, 10, 22), fri, date(2026, 10, 26)]


def _steps(reports_status: str | None) -> str:
    steps = [{"name": "dbt", "status": "ok"}]
    if reports_status:
        steps.append({"name": "reports", "status": reports_status})
    return json.dumps(steps)


def test_published_runs_needs_an_ok_reports_step():
    t = pd.Timestamp("2026-10-22 10:00")
    runs = pd.DataFrame({
        "status": ["ok", "ok", "failed", "ok"],
        "steps": [_steps("ok"), _steps(None), _steps("ok"), _steps("skipped")],
        "finished_at": [t, t, t, t],
    })
    pub = ops.published_runs(runs)
    assert len(pub) == 1 and pub["finished_local"].iloc[0].tzinfo is not None


# --------------------------------------------------------------------------- ops page
AUDIT_DDL = """
create schema audit;
create table audit.pipeline_run (run_id varchar, started_at timestamp, finished_at timestamp, status varchar,
    seconds double, steps varchar, error varchar, args varchar);
create table audit.diagnostics_result (run_id varchar, check_name varchar, status varchar, failures bigint,
    measured_value double, threshold double, message varchar, checked_at timestamp);
create table audit.reconciliation (run_id varchar, source_system varchar, entity varchar, measure varchar,
    comparison varchar, expected_rows bigint, actual_rows bigint, expected_amount double, actual_amount double,
    row_diff bigint, amount_diff double, status varchar, checked_at timestamp);
"""


def _audit_con(diag_status: str = "pass", recon_status: str = "pass") -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute(AUDIT_DDL)
    fin = datetime.now(TZ).astimezone(ops.UTC).replace(tzinfo=None) - timedelta(hours=1)
    con.execute("insert into audit.pipeline_run values ('r1', ?, ?, 'ok', 120, ?, '', '{}')",
                [fin - timedelta(minutes=2), fin, _steps("ok")])
    con.execute("insert into audit.diagnostics_result values ('r1', 'diag_fan_out_guard', ?, 0, 0, 0, '', now())",
                [diag_status])
    con.execute("insert into audit.reconciliation values ('r1', 'admin_core', 'contracts', 'coverage_cost', "
                "'source -> raw', 1, 1, 1, 1, 0, 0, ?, now())", [recon_status])
    return con


def test_ops_page_from_audit_tables(tmp_path):
    (tmp_path / "dealers" / "T1" / "D1").mkdir(parents=True)
    (tmp_path / "dealers" / "T1" / "D1" / "index.html").write_text("<html></html>", encoding="utf-8")
    [page] = ops.build(_audit_con(), tmp_path)
    assert page.path == "ops.html"
    kpis = {k.label: k for k in page.kpis}
    assert kpis["Last pipeline run"].value == "OK"
    assert kpis["Diagnostics passing"].value == "1 / 1" and kpis["Diagnostics passing"].status == "good"
    assert kpis["Reconciliation breaks"].value == "0"
    titles = [s.title for s in page.sections]
    assert "Dealer packets" in titles
    packet_rows = page.sections[titles.index("Dealer packets")].rows
    assert packet_rows[0][2]["href"] == "dealers/T1/D1/index.html"


def test_ops_page_flags_failed_gates(tmp_path):
    [page] = ops.build(_audit_con("fail", "break"), tmp_path)
    kpis = {k.label: k for k in page.kpis}
    assert kpis["Diagnostics passing"].status == "critical"
    assert kpis["Reconciliation breaks"].value == "1" and kpis["Reconciliation breaks"].status == "critical"


def test_ops_page_skips_without_audit_schema(tmp_path):
    assert ops.build(duckdb.connect(), tmp_path) == []


# --------------------------------------------------------------------------- plug-in hook
def _fake_plugin(name: str, pages=None, exc: Exception | None = None, label: str | None = None):
    mod = types.ModuleType(name)

    def build(con, out_dir):
        if exc:
            raise exc
        (out_dir / "downloads" / "fake").mkdir(parents=True, exist_ok=True)
        (out_dir / "downloads" / "fake" / "x.xlsx").write_bytes(b"x")
        return pages

    mod.build = build
    if label:
        mod.NAV_LABEL = label
    return mod


def test_plugin_pages_collects_pages_and_nav(tmp_path):
    page = rb.Page("fake/index.html", "Fake", "lede")
    pages, nav = rb.plugin_pages(None, tmp_path, [("fake", _fake_plugin("reports.fake", [page], label="Fake area"))])
    assert pages == [page] and nav == [("fake/index.html", "Fake area")]
    pages, nav = rb.plugin_pages(None, tmp_path, [("empty", _fake_plugin("reports.empty", []))])
    assert pages == [] and nav == []


def test_plugin_failure_names_the_area(tmp_path):
    with pytest.raises(RuntimeError, match="reports.boom"):
        rb.plugin_pages(None, tmp_path, [("boom", _fake_plugin("reports.boom", exc=ValueError("bad mart")))])
    with pytest.raises(TypeError, match="Page"):
        rb.plugin_pages(None, tmp_path, [("bad", _fake_plugin("reports.bad", [{"path": "x.html"}]))])


def test_discover_plugins_imports_existing_areas(monkeypatch):
    monkeypatch.setitem(sys.modules, "reports.zz_fake", _fake_plugin("reports.zz_fake", []))
    monkeypatch.setattr(rb.importlib.util, "find_spec",
                        lambda name: object() if name in ("reports.zz_fake", "reports.ops") else None)
    found = dict(rb.discover_plugins(("zz_fake", "missing", "ops")))
    assert set(found) == {"zz_fake", "ops"} and found["ops"] is ops
    assert "ops" in rb.PLUGIN_AREAS and {"regulatory", "dealer", "claims", "finance"} <= set(rb.PLUGIN_AREAS)


def test_copy_dealer_packets(tmp_path):
    pub = tmp_path / "publish"
    for tenant, dealer in [("T1", "D1"), ("T1", "D2"), ("T2", "D9")]:
        d = pub / "dealers" / tenant / dealer
        d.mkdir(parents=True)
        (d / "index.html").write_text("<html></html>", encoding="utf-8")
        (d / "production.csv").write_text("a\n1\n", encoding="utf-8")
    out = tmp_path / "site"
    assert rb.copy_dealer_packets(pub, out) == 3
    assert (out / "dealers" / "T2" / "D9" / "production.csv").is_file()
    assert rb.copy_dealer_packets(None, out) == 0 and rb.copy_dealer_packets(tmp_path / "nope", out) == 0


def test_render_site_adds_plugin_pages_nav_and_packets(tmp_path, monkeypatch):
    """The whole hook: packets copied, plug-in pages rendered with nav, and every link resolves."""
    monkeypatch.setattr(rb, "build_pages", lambda d: [rb.Page("index.html", "Overview", "core page")])
    monkeypatch.setattr(rb, "NAV", [("index.html", "Overview")])
    pub = tmp_path / "publish"
    (pub / "dealers" / "T1" / "D1").mkdir(parents=True)
    (pub / "dealers" / "T1" / "D1" / "index.html").write_text("<html></html>", encoding="utf-8")
    child = rb.Page("fake/detail.html", "Detail", "lede", nav="fake/index.html", crumb="All fake")
    plugin = _fake_plugin("reports.fake", [rb.Page("fake/index.html", "Fake", "lede"), child], label="Fake")
    d = SimpleNamespace(as_of=date(2026, 10, 22), last_month=date(2026, 9, 1))
    out = tmp_path / "site"
    written = rb.render_site(d, out, con=_audit_con(), publish_dir=pub,
                             plugins=[("fake", plugin), ("ops", ops)])
    assert {p.relative_to(out).as_posix() for p in written} == {
        "index.html", "fake/index.html", "fake/detail.html", "ops.html"}
    index = (out / "index.html").read_text(encoding="utf-8")
    assert 'href="fake/index.html"' in index and ">Operations<" in index
    assert "All fake" in (out / "fake" / "detail.html").read_text(encoding="utf-8")
    assert (out / "downloads" / "fake" / "x.xlsx").is_file()
    assert 'href="dealers/T1/D1/index.html"' in (out / "ops.html").read_text(encoding="utf-8")
    assert broken_links(out) == []


def test_render_site_without_connection_keeps_v1_behaviour(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "build_pages", lambda d: [rb.Page("index.html", "Overview", "core page")])
    monkeypatch.setattr(rb, "NAV", [("index.html", "Overview")])
    d = SimpleNamespace(as_of=date(2026, 10, 22), last_month=date(2026, 9, 1))
    written = rb.render_site(d, tmp_path / "site")
    assert [p.name for p in written] == ["index.html"]
    assert "Operations" not in (tmp_path / "site" / "index.html").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- data dictionary
def test_dictionary_reads_dbt_yaml():
    models = dictionary.load_yaml_models()
    assert "dim_contract" in models and models["dim_contract"].folder == "models/marts/core"
    assert models["dim_contract"].columns["contract_key"].tests  # unique / not_null from _core.yml
    assert any(m.meta.get("contains_pii") for m in models.values())


def test_dictionary_render_uses_warehouse_types_and_hides_metadata():
    models = {"dim_x": dictionary.Model("dim_x", "models/marts/core", "A dim.", {"grain": "x"},
                                        {"x_key": dictionary.Column("x_key", tests=["unique"])}),
              "stg_a__b": dictionary.Model("stg_a__b", "models/staging/a", "Staged.", {"contains_pii": True})}
    wh = {("core", "dim_x"): [("x_key", "VARCHAR"), ("label", "VARCHAR"), ("posting_period_key", "INTEGER"),
                              ("_built_at", "TIMESTAMP")]}
    md = dictionary.render(models, wh)
    assert "### `core.dim_x`" in md and "grain: x" in md
    assert "| `x_key` | VARCHAR |" in md and "unique" in md
    assert "| `label` | VARCHAR | *undocumented* |" in md
    assert "yyyymm" in md.split("`posting_period_key` | INTEGER |")[1].splitlines()[0]
    assert "_built_at" not in md.split("## Core")[1]
    assert "`stg_a__b`" in md and "contains PII" in md
    assert "Column coverage: 2 of 3" in md


def test_generated_dictionary_is_committed():
    text = (OPS_DOCS / "DATA_DICTIONARY.md").read_text(encoding="utf-8")
    assert "python -m reports.dictionary" in text and "`core.dim_contract`" in text


# --------------------------------------------------------------------------- metric catalog (docs/METRICS.md)
def test_metric_catalog_definitions(tmp_path):
    yml = tmp_path / "metrics.yml"
    yml.write_text("""
semantic_models:
  - name: s
    model: ref('fct_x')
    measures:
      - {name: paid, agg: sum, expr: paid_amount}
      - {name: lines, agg: count, expr: line_key}
metrics:
  - {name: paid, label: Paid, type: simple, type_params: {measure: paid}, config: {meta: {pbi: {folder: Claims}}}}
  - {name: lines, label: Lines, type: simple, type_params: {measure: lines}}
  - {name: avg_paid, label: Avg, type: ratio, type_params: {numerator: paid, denominator: {name: lines}}}
  - {name: gap, label: Gap, type: derived, type_params: {expr: paid - lines, metrics: [{name: paid}]}}
""", encoding="utf-8")
    rows = {r["name"]: r for r in dictionary.metric_catalog(yml)}
    assert rows["paid"]["definition"] == "sum(paid_amount)" and rows["paid"]["source"] == "fct_x"
    assert rows["paid"]["folder"] == "Claims"
    assert rows["avg_paid"]["definition"] == "paid / lines"
    assert rows["gap"]["definition"] == "paid - lines"
    md = tmp_path / "METRICS.md"
    md.write_text(f"# M\n\nintro\n\n{dictionary.CATALOG_START}\nstale\n{dictionary.CATALOG_END}\n", encoding="utf-8")
    assert dictionary.update_metrics_md(md, yml) is True
    text = md.read_text(encoding="utf-8")
    assert "stale" not in text and "| `avg_paid` |" in text and text.startswith("# M\n\nintro")
    assert dictionary.update_metrics_md(md, yml) is False  # idempotent


def test_metrics_md_catalog_is_in_sync_with_metrics_yml():
    """Metric rule: a formula is defined once (metrics.yml) and METRICS.md shows exactly that.
    If this fails after a metrics.yml change, run: python -m reports.dictionary"""
    text = (ROOT / "docs" / "METRICS.md").read_text(encoding="utf-8")
    block = text.split(dictionary.CATALOG_START, 1)[1].split(dictionary.CATALOG_END, 1)[0]
    expected = dictionary.render_metric_catalog(dictionary.metric_catalog())
    assert dictionary.CATALOG_START + block + dictionary.CATALOG_END == expected


# --------------------------------------------------------------------------- the site sees its own run
def test_ops_page_shows_the_run_publishing_now(tmp_path, monkeypatch):
    """The reports step renders while this run's row is still 'running': the page shows it as OK and
    published at the render time, never IN PROGRESS."""
    con = _audit_con()
    fin = datetime.now(ops.UTC).replace(tzinfo=None)
    con.execute("insert into audit.pipeline_run values ('r2', ?, ?, 'running', 60, ?, '', '{}')",
                [fin - timedelta(minutes=1), fin, _steps(None)])
    monkeypatch.setenv("PIPELINE_RUN_ID", "r2")
    [page] = ops.build(con, tmp_path)
    kpi = {k.label: k for k in page.kpis}["Last pipeline run"]
    assert kpi.value == "OK" and kpi.status == "good" and "published" in kpi.sub and "local" in kpi.sub
    monkeypatch.setenv("PIPELINE_RUN_ID", "other")  # a 'running' row of another run never finished
    kpi = {k.label: k for k in ops.build(con, tmp_path)[0].kpis}["Last pipeline run"]
    assert kpi.value == "INCOMPLETE" and kpi.status == "critical"


def test_completed_run_site_counts_today_met_and_never_says_in_progress(tmp_path, monkeypatch):
    """Run 20261023T073105Z shape: gates pass, the reports step renders at 02:34 Chicago (before 07:00).
    The published ops and quality pages show the run as OK and Friday's SLO day as met, not pending."""
    from reports import data as rd

    now = datetime(2026, 10, 23, 2, 34, tzinfo=ops.SLA_TZ)  # a Friday
    run_id = "20261023T073105Z"
    con = duckdb.connect()
    con.execute(AUDIT_DDL)
    con.execute("insert into audit.pipeline_run values (?, ?, ?, 'running', 180, ?, '', '{}')",
                [run_id, datetime(2026, 10, 23, 7, 31, 5), datetime(2026, 10, 23, 7, 34), _steps(None)])
    monkeypatch.setenv("PIPELINE_RUN_ID", run_id)
    [page] = ops.build(con, tmp_path, now=now)
    sla = page.sections[0]
    today = next(r for r in sla.rows if r[0] == "Fri Oct 23")
    assert today[1]["text"] == "Met" and today[2] == "Oct 23 02:34"
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / f"{run_id}.json").write_text(json.dumps({"run_id": run_id, "status": "running", "steps": [
        {"name": "dbt", "status": "ok", "seconds": 60.0}]}), "utf-8")
    d = SimpleNamespace(
        quality={"dbt": {"counts": {"test": {"pass": 1}}}, "last_run": rd._last_run(runs), "ingest": pd.DataFrame(
            columns=["table_name", "watermark_to", "rows_read", "rows_loaded", "rows_quarantined", "status"]),
            "flags": [], "tables": pd.DataFrame(columns=["layer", "table_name", "rows"])},
        as_of=date(2026, 10, 22), manifest={"rules": {"min_contracts": 5, "min_dealers": 3, "max_dealer_share": .5},
                                            "tables": {}})
    for p in (page, rb.page_quality(d)):
        tile = {k.label: k for k in p.kpis}["Last pipeline run"]
        assert tile.value == "OK" and tile.status == "good"
        assert "IN PROGRESS" not in json.dumps([vars(k) for k in p.kpis], default=str)


def _rr(path: Path, generated_at: str, kinds: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    results = [{"unique_id": f"{k}.vsc.n{i}", "status": "success" if k == "model" else "pass"}
               for k, n in kinds.items() for i in range(n)]
    path.write_text(json.dumps({"metadata": {"generated_at": generated_at}, "elapsed_time": 9.0,
                                "results": results}), encoding="utf-8")


def test_quality_reads_the_saved_dbt_build_not_the_gate_results(tmp_path):
    from reports import data as rd

    target, runs = tmp_path / "target", tmp_path / "runs"
    _rr(runs / "RUN1" / "dbt_build.json", "2026-10-23T05:16:00Z", {"model": 132, "test": 270})
    _rr(target / "run_results.json", "2026-10-23T05:17:00Z", {"test": 5})  # reconcile's dbt test
    out = rd._dbt_results(target, runs)
    assert out["counts"]["model"]["success"] == 132 and out["counts"]["test"]["pass"] == 270
    _rr(target / "run_results.json", "2026-10-23T06:00:00Z", {"model": 3, "test": 4})  # newer manual build wins
    assert rd._dbt_results(target, runs)["counts"]["model"]["success"] == 3


def test_last_run_includes_the_run_building_the_site(tmp_path, monkeypatch):
    from reports import data as rd

    (tmp_path / "RUN1.json").write_text(json.dumps({"run_id": "RUN1", "status": "ok", "steps": []}), "utf-8")
    (tmp_path / "RUN2.json").write_text(json.dumps({"run_id": "RUN2", "status": "running", "steps": []}), "utf-8")
    monkeypatch.delenv("PIPELINE_RUN_ID", raising=False)
    assert rd._last_run(tmp_path)["run_id"] == "RUN1"  # a stale 'running' file is not this run
    monkeypatch.setenv("PIPELINE_RUN_ID", "RUN2")
    run = rd._last_run(tmp_path)
    assert run["run_id"] == "RUN2"
    d = SimpleNamespace(
        quality={"dbt": {"counts": {"test": {"pass": 270}}}, "last_run": run, "ingest": pd.DataFrame(
            columns=["table_name", "watermark_to", "rows_read", "rows_loaded", "rows_quarantined", "status"]),
            "flags": [], "tables": pd.DataFrame(columns=["layer", "table_name", "rows"])},
        as_of=date(2026, 10, 22), manifest={"rules": {"min_contracts": 5, "min_dealers": 3, "max_dealer_share": .5},
                                            "tables": {}})
    kpis = {k.label: k for k in rb.page_quality(d).kpis}
    assert kpis["Last pipeline run"].value == "OK" and kpis["dbt tests passing"].value == "270 / 270"


def test_waived_reconciliation_rows_are_not_breaks(tmp_path):
    con = _audit_con()
    con.execute("insert into audit.reconciliation values ('r1', 'migration', 'CONTRACT_IN_FORCE_AT_CUTOVER', "
                "'rows/$', 'legacy -> admin_core', 10, 0, 5, 0, -10, -5, 'not_migrated', now())")
    [page] = ops.build(con, tmp_path)
    kpi = {k.label: k for k in page.kpis}["Reconciliation breaks"]
    assert kpi.value == "0" and kpi.status == "good"
