"""Diagnostics gate: result parsing, audit rows, and (with a built warehouse) a real injected failure."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from pipeline import diagnostics as dg

ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))
DIAG_DIR = ROOT / "dbt" / "tests" / "diagnostics"
EXPECTED = {
    "diag_product_dim_match_rate", "diag_contract_key_bridge_coverage", "diag_claim_contract_coverage",
    "diag_fan_out_guard", "diag_claim_sign", "diag_no_demo_rows", "diag_freshness", "diag_posting_period_integrity",
}


def _results(tmp_path: Path, rows: list[tuple[str, str, int]]) -> Path:
    path = tmp_path / "run_results.json"
    path.write_text(json.dumps({"results": [
        {"unique_id": f"test.warranty.{name}", "status": status, "failures": failures, "message": None}
        for name, status, failures in rows
    ]}), encoding="utf-8")
    return path


def test_every_design_check_has_a_test_and_a_measure():
    assert {p.stem for p in DIAG_DIR.glob("*.sql")} == EXPECTED
    assert set(dg.MEASURES) == EXPECTED


def test_parse_run_results_maps_statuses(tmp_path):
    path = _results(tmp_path, [("diag_a", "pass", 0), ("diag_b", "fail", 3), ("diag_c", "error", 0),
                               ("not_a_diag", "fail", 9)])
    got = {r.check_name: (r.status, r.failures) for r in dg.parse_run_results(path, "diag_")}
    assert got == {"diag_a": ("pass", 0), "diag_b": ("fail", 3), "diag_c": ("error", 0)}


def test_gate_with_no_results_cannot_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("DBT_TARGET_PATH", str(tmp_path))
    with pytest.raises(dg.GateError):
        dg.run_dbt_gate("tag:diagnostics", "diag_", runner=lambda argv, env: 2)


def test_run_writes_audit_rows_and_reports_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("DBT_TARGET_PATH", str(tmp_path))
    monkeypatch.setenv("PIPELINE_RUN_ID", "RUN-T")

    def fake_dbt(argv, env):
        assert "tag:diagnostics" in argv
        _results(tmp_path, [("diag_fan_out_guard", "pass", 0), ("diag_claim_sign", "fail", 2)])
        return 1

    wh = tmp_path / "wh.duckdb"
    results = dg.run(wh, runner=fake_dbt)
    assert dg.report(results, "diagnostics") == 1
    with duckdb.connect(str(wh)) as con:
        rows = con.execute("select run_id, check_name, status, failures from audit.diagnostics_result "
                           "order by check_name").fetchall()
    assert rows == [("RUN-T", "diag_claim_sign", "fail", 2), ("RUN-T", "diag_fan_out_guard", "pass", 0)]
    assert dg.report([r for r in results if r.ok], "diagnostics") == 0


# --------------------------------------------------------------------------- against a built warehouse
def _has_core() -> bool:
    if not WAREHOUSE.exists():
        return False
    try:
        with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
            con.execute("select 1 from core.dim_contract limit 1")
        return True
    except duckdb.Error:
        return False


needs_warehouse = pytest.mark.skipif(not _has_core(), reason="warehouse not built (run python -m pipeline.run)")


@needs_warehouse
def test_measures_run_and_pass_thresholds_on_the_built_warehouse():
    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        values = {name: con.execute(sql).fetchone()[0] for name, (sql, _) in dg.MEASURES.items()}
    assert values["diag_product_dim_match_rate"] >= 0.995
    assert values["diag_claim_contract_coverage"] >= 0.98
    assert values["diag_fan_out_guard"] == 0 and values["diag_no_demo_rows"] == 0
    assert values["diag_claim_sign"] >= 0


@needs_warehouse
def test_injected_failure_fails_the_real_dbt_diagnostic(tmp_path):
    """Raise the product match-rate bar above 100%: the real dbt diagnostic must fail."""
    env = {**os.environ, "WAREHOUSE_PATH": str(WAREHOUSE), "DBT_TARGET_PATH": str(tmp_path / "target"),
           "DBT_LOG_PATH": str(tmp_path / "logs"), "DO_NOT_TRACK": "1"}
    cmd = dg.dbt_test_command("diag_product_dim_match_rate", ["--vars", '{"min_product_match_rate": 1.01}'])
    assert cmd[0] == sys.executable
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert proc.returncode != 0
    results = dg.parse_run_results(tmp_path / "target" / "run_results.json", "diag_")
    assert [(r.check_name, r.status) for r in results] == [("diag_product_dim_match_rate", "fail")]


# --------------------------------------------------------------------------- layer row counts
def test_layer_of_maps_schemas_to_layers():
    assert dg.layer_of("raw_admin_core", "source") == "raw"
    assert dg.layer_of("audit", "source") is None
    assert dg.layer_of("staging", "model") == "staging"
    assert dg.layer_of("intermediate", "model") == "intermediate"
    assert dg.layer_of("snapshots", "snapshot") == "snapshots"
    assert dg.layer_of("finance", "model") == "marts"
    assert dg.layer_of("gold", "model") is None and dg.layer_of("seeds", "seed") is None


def test_record_layer_counts_writes_one_row_per_manifest_relation(tmp_path):
    manifest = {
        "nodes": {
            "model.w.stg_a": {"resource_type": "model", "schema": "staging", "alias": "stg_a",
                              "config": {"materialized": "view"}, "tags": []},
            "model.w.fct_b": {"resource_type": "model", "schema": "core", "alias": "fct_b",
                              "config": {"materialized": "table"}, "tags": []},
            "model.w.v1": {"resource_type": "model", "schema": "gold", "alias": "fct_claim",
                           "config": {"materialized": "view"}, "tags": ["v1_compat"]},
            "model.w.gone": {"resource_type": "model", "schema": "core", "alias": "not_built",
                             "config": {"materialized": "table"}, "tags": []},
            "test.w.t": {"resource_type": "test", "schema": "core", "alias": "t", "config": {}},
        },
        "sources": {"source.w.raw.x": {"resource_type": "source", "schema": "raw_admin_core", "identifier": "x",
                                       "config": {}}},
    }
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    con = duckdb.connect()
    con.execute("create schema staging; create schema core; create schema raw_admin_core; create schema gold")
    con.execute("create table raw_admin_core.x as select range as id from range(5)")
    con.execute("create view staging.stg_a as select * from raw_admin_core.x where id < 3")
    con.execute("create table core.fct_b as select 1 as k")
    con.execute("create view gold.fct_claim as select 1 as k")
    assert dg.record_layer_counts(con, "R1", path) == 3  # not_built is skipped, v1_compat excluded
    got = con.execute("select layer, relation_name, row_count from audit.layer_row_count order by 1").fetchall()
    assert got == [("marts", "fct_b", 1), ("raw", "x", 5), ("staging", "stg_a", 3)]
    assert dg.record_layer_counts(con, "R1", path) == 3  # a re-run replaces, never duplicates
    assert con.execute("select count(*) from audit.layer_row_count").fetchone()[0] == 3
    assert dg.record_layer_counts(con, "R2", tmp_path / "missing.json") == 0
