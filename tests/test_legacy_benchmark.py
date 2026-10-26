"""python -m legacy demo: the benchmark runner, the pain-point catalog, the doc and the site page.

Offline tests use a small made-up benchmark JSON. The live tests need PostgreSQL and skip without it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legacy import PLAYBOOK_DIR, benchmark, report

BENCH = PLAYBOOK_DIR / "benchmark"


def _side(cols, row, ms=10.0, **kw):
    return {"columns": cols, "first_row": row, "answer": row[0] if row else None, "ms": ms, "rows": 1,
            "timed_out": False, "error": None, "plan_top": ["Seq Scan on t (rows=5, 1 ms)"],
            "plan_scans": {"Seq Scan": 1}, "plan_kind": "analyze", "file": "x.sql", **kw}


def _fake() -> dict:
    qs = [
        {"id": "q01", "question": "dealer", "expected_failure": "lost", "observed_failure": "lost", "diff": -1,
         "legacy": _side(["in_force_contracts", "coverage_cost", "in_force_all_identities",
                          "dealer_ids_for_one_dealer"], [5, 100.0, 9, 3]),
         "modern": _side(["in_force_contracts", "coverage_cost"], [9, 180.0])},
        {"id": "q07", "question": "spine", "expected_failure": "timeout", "observed_failure": "timeout",
         "diff": None,
         "legacy": _side([], None, ms=2000.0, timed_out=True, note="timed out at 2 s", timeout="2s",
                         plan_kind="estimated (no ANALYZE: the query hit its timeout)"),
         "modern": _side(["contracts"], [7], ms=3.0)},
        {"id": "q09", "question": "eav", "expected_failure": "extra step", "observed_failure": "extra step",
         "diff": 0,
         "legacy_steps": [_side([], None, error_expected=True, error_text="cross-database references are not "
                                "implemented"),
                          _side(["contracts_with_chassis_make", "contracts_with_any_field",
                                 "contracts_missing_a_field"], [4, 4, 1])],
         "legacy": _side(["contracts_with_chassis_make", "contracts_with_any_field", "contracts_missing_a_field"],
                         [4, 4, 1]),
         "modern": _side(["contracts_with_chassis_make", "states"], [4, 2])},
    ]
    return {"generated_at": "2026-01-01T00:00:00Z", "legacy_database": "vsc_legacy", "modern_database": "m",
            "questions": qs, "skipped": ["q02"],
            "clutter": {"legacy": {"findings": 2, "by_rule": {"backup_name": 2}, "items": ["a", "b"]},
                        "modern": {"findings": 0, "by_rule": {}, "items": []}}}


def test_every_question_has_sql_both_ways_and_headers():
    for qid in benchmark.QUESTIONS:
        steps = benchmark.legacy_steps(BENCH, qid)
        assert steps, f"{qid}: no legacy file"
        assert (BENCH / f"{qid}_modern.sql").exists(), f"{qid}: no modern file"
        for f in [*steps, BENCH / f"{qid}_modern.sql"]:
            sql = f.read_text(encoding="utf-8")
            h = benchmark.headers(sql)
            assert h.get("database") in ("legacy", "legacy_admin", "modern"), f.name
            assert benchmark.seconds(h["timeout"]) > 0, f.name
            assert benchmark.text_headers(sql).get("question"), f"{f.name}: no question header"
            assert "set statement_timeout" in sql, f.name


def test_legacy_steps_order_and_expect_error_header(tmp_path: Path):
    for name in ("q05_legacy.sql", "q05_legacy_10.sql", "q05_legacy_2.sql", "q05_modern.sql"):
        (tmp_path / name).write_text("select 1;", encoding="utf-8")
    assert [p.name for p in benchmark.legacy_steps(tmp_path, "q05")] == [
        "q05_legacy.sql", "q05_legacy_2.sql", "q05_legacy_10.sql"]
    h = benchmark.headers("-- database: legacy\n-- timeout: 2s\n-- expect_error: does not exist\nselect 1;")
    assert h == {"database": "legacy", "timeout": "2s", "expect_error": "does not exist"}
    assert benchmark.seconds("2s") == 2 and benchmark.seconds("5min") == 300


def test_plan_nodes_label_actuals():
    plan = {"Node Type": "Aggregate", "Actual Rows": 1, "Actual Loops": 1, "Actual Total Time": 12.0,
            "Plans": [{"Node Type": "Seq Scan", "Relation Name": "t", "Schema": "s", "Actual Rows": 10,
                       "Actual Loops": 2, "Actual Total Time": 3.0}]}
    assert benchmark._plan_nodes(plan) == ["Aggregate (rows=1, 12 ms)", "Seq Scan on s.t (rows=20, 3 ms)"]
    assert benchmark._scan_kinds(plan) == {"Seq Scan": 1}


def test_classify_and_errors():
    e = {"legacy": {"timed_out": True}, "modern": {}, "diff": None}
    assert benchmark.classify(e, "lost") == "timeout"
    assert benchmark.classify({"legacy": {"ms": 900.0}, "modern": {"ms": 10.0}, "diff": 1}, "timeout") == "slow"
    assert benchmark.classify({"legacy": {"ms": 20.0}, "modern": {"ms": 10.0}, "diff": 1}, "timeout") == "none"
    e = {"legacy": {}, "modern": {}, "diff": 0}
    assert benchmark.classify(e, "lost") == "none"
    e = {"legacy": {}, "modern": {}, "diff": 0, "legacy_steps": [{"error_expected": True}, {}]}
    assert benchmark.classify(e, "extra step") == "extra step"
    e = {"legacy": {"error": "boom"}, "modern": {}, "diff": None}
    assert benchmark.classify(e, "lost") == "error"
    data = _fake()
    assert benchmark.errors(data) == []
    data["questions"][0]["modern"]["error"] = "relation does not exist"
    assert len(benchmark.errors(data)) == 1


def test_catalog_covers_every_pain_point_with_a_fix():
    assert [p.num for p in report.PAIN_POINTS] == list(range(1, 13))
    for p in report.PAIN_POINTS:
        assert p.what and p.root_cause and p.fix and p.where, p.num
        assert set(p.questions) <= set(benchmark.QUESTIONS), p.num
    measured = {q for p in report.PAIN_POINTS for q in p.questions}
    assert measured == set(benchmark.QUESTIONS)


def test_markdown_reads_numbers_from_the_json():
    md = report.markdown(_fake())
    assert "Measured 2026-01-01T00:00:00Z" in md
    assert "found 5 in-force contracts" in md and "holds 9" in md and "4 were lost" in md
    assert "timed out at 2 s" in md
    assert "cross-database references are not implemented" in md
    assert "2 findings in the legacy database" in md
    assert md.count("- **The fix:**") == len(report.PAIN_POINTS)
    assert "not measured in the last run" in md  # q02 etc. were skipped in the fake run
    assert "Skipped (no SQL pair yet): q02" in md


def test_write_and_load_snapshot(tmp_path: Path):
    doc, snap = tmp_path / "B.md", tmp_path / "snap.json"
    report.write(_fake(), doc=doc, snapshot=snap)
    assert doc.read_text(encoding="utf-8").startswith("# Legacy benchmark")
    assert report.load(snap)["generated_at"] == "2026-01-01T00:00:00Z"


def test_committed_snapshot_and_doc_agree():
    if not report.SNAPSHOT.exists():
        pytest.skip("no committed benchmark snapshot yet")
    data = json.loads(report.SNAPSHOT.read_text(encoding="utf-8"))
    assert report.DOC.read_text(encoding="utf-8") == report.markdown(data)


def test_site_page_renders_one_card_per_pain_point(tmp_path: Path, monkeypatch):
    import reports.legacy as page

    monkeypatch.setattr(report, "load", lambda path=None: _fake())
    monkeypatch.setattr(report, "STATE_JSON", tmp_path / "none.json")
    [p] = page.build(None, tmp_path)
    assert p.path == "legacy.html" and p.title == "Legacy vs better"
    cards = [s for s in p.sections if "pain-point" in s.html]
    assert len(cards) == len(report.PAIN_POINTS)
    assert all("Root cause in the foundation" in s.html and "Where it lives" in s.html for s in cards)
    assert p.sections[0].rows and p.sections[0].rows[0][0] == "Q01"
    assert page.NAV_GROUP == "Platform"


def test_site_page_without_any_measurement(tmp_path: Path, monkeypatch):
    import reports.legacy as page

    monkeypatch.setattr(report, "load", lambda path=None: None)
    [p] = page.build(None, tmp_path)
    assert "python -m legacy demo" in p.lede


def test_plugin_is_registered_in_platform():
    import reports.build as rb

    assert "legacy" in rb.PLUGIN_AREAS and rb.AREA_GROUPS["legacy"] == "Platform"


# ---------------------------------------------------------------------- live (PostgreSQL)
def _pg() -> str | None:
    from warehouse.postgres.sync import available

    return available()


@pytest.mark.skipif(_pg() is not None, reason="PostgreSQL not available")
def test_run_sql_timeout_is_reported_and_never_analyzed():
    from warehouse.postgres.sync import Settings

    s = Settings.from_env()
    r = benchmark.run_sql(s, "postgres", "select pg_sleep(5);", "1s")
    assert r["timed_out"] and r["note"] == "timed out at 1 s" and r["ms"] < 4000
    assert r["plan_kind"].startswith("estimated")
    ok = benchmark.run_sql(s, "postgres", "set statement_timeout = '5s';\nselect 42 as answer;", "5s")
    assert ok["answer"] == 42 and ok["plan_kind"] == "analyze" and ok["columns"] == ["answer"]
    err = benchmark.run_sql(s, "postgres", "select * from no_such_table;", "5s", expect_error="does not exist")
    assert err["error"] is None and err["error_expected"]
