"""Orchestrator tests. Steps are mocked: no subprocesses, no data, no Airflow install needed."""

from __future__ import annotations

import ast
import json
import py_compile
from pathlib import Path

import duckdb
import pytest

from pipeline import run as pr

ROOT = Path(__file__).resolve().parents[1]
DAG = ROOT / "pipeline" / "airflow" / "dags" / "vsc_daily.py"
ALL_MODULES = ["generator", "ingest.feed", "ingest", "dbt", "pipeline.diagnostics", "pipeline.reconcile",
               "actuarial", "semantic", "publish", "reports"]


class FakeRunner:
    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.fail_on = fail_on

    def __call__(self, argv, env) -> int:
        self.calls.append(list(argv))
        self.envs.append(env)
        assert "WAREHOUSE_PATH" in env
        return 2 if self.fail_on and self.fail_on in argv else 0


def _step_names(calls: list[list[str]]) -> list[str]:
    """Step modules in call order; the dbt step's follow-up ``dbt source freshness`` is not a step."""
    return ["dbt" if c[2] == "dbt.cli.main" else c[2] for c in calls if not _is_freshness(c)]


def _is_freshness(call: list[str]) -> bool:
    return call[2] == "dbt.cli.main" and call[3:5] == ["source", "freshness"]


@pytest.fixture(autouse=True)
def all_modules_present(monkeypatch, tmp_path):
    """Pretend optional step modules (semantic/) exist, and keep the warehouse out of the way."""
    marker = tmp_path / "semantic_main.py"
    marker.write_text("")
    monkeypatch.setattr(pr, "OPTIONAL_MODULES", {"semantic": marker})
    monkeypatch.setenv("WAREHOUSE_PATH", str(tmp_path / "no_warehouse.duckdb"))


@pytest.fixture
def no_source(monkeypatch, tmp_path):
    monkeypatch.setattr(pr, "ADMIN_CORE_DB", tmp_path / "missing.db")


@pytest.fixture
def has_source(monkeypatch, tmp_path):
    db = tmp_path / "admin_core.db"
    db.write_bytes(b"")
    monkeypatch.setattr(pr, "ADMIN_CORE_DB", db)


def test_steps_match_the_design_order():
    assert pr.STEPS == ("generate", "feed", "ingest", "dbt", "diagnostics", "reconcile", "actuarial", "semantic",
                        "publish", "reports")
    assert pr.GATES == ("dbt", "diagnostics", "reconcile")


def test_full_run_order_and_summary(no_source, tmp_path):
    runner = FakeRunner()
    summary = pr.run_pipeline(pr.parse_args([]), runner, runs_dir=tmp_path / "runs")
    assert summary.status == "ok"
    assert _step_names(runner.calls) == ALL_MODULES
    written = json.loads((tmp_path / "runs" / f"{summary.run_id}.json").read_text())
    assert written["status"] == "ok" and len(written["steps"]) == 10
    gates = json.loads((tmp_path / "runs" / pr.GATES_FILE).read_text())
    assert {g: v["status"] for g, v in gates.items()} == {"dbt": "ok", "diagnostics": "ok", "reconcile": "ok"}


def test_generate_skipped_when_source_exists(has_source, tmp_path):
    runner = FakeRunner()
    summary = pr.run_pipeline(pr.parse_args([]), runner, runs_dir=tmp_path)
    assert summary.steps[0].status == "skipped"
    assert "generator" not in _step_names(runner.calls)
    assert _step_names(runner.calls)[0] == "ingest.feed"


def test_append_day_forces_generate(has_source, tmp_path):
    runner = FakeRunner()
    pr.run_pipeline(pr.parse_args(["--append-day", "3"]), runner, runs_dir=tmp_path)
    assert runner.calls[0][2:] == ["generator", "--append-day", "3"]
    assert _step_names(runner.calls)[1:3] == ["ingest.feed", "ingest"]


def test_fail_fast(has_source, tmp_path):
    runner = FakeRunner(fail_on="dbt.cli.main")
    summary = pr.run_pipeline(pr.parse_args([]), runner, runs_dir=tmp_path)
    assert summary.status == "failed"
    assert "--from dbt" in summary.error
    assert _step_names(runner.calls) == ["ingest.feed", "ingest", "dbt"]
    assert summary.steps[-1].returncode == 2
    assert json.loads((tmp_path / pr.GATES_FILE).read_text())["dbt"]["status"] == "failed"


@pytest.mark.parametrize("gate", ["pipeline.diagnostics", "pipeline.reconcile"])
def test_failed_gate_blocks_publishing(has_source, tmp_path, gate):
    """Injected gate failure: nothing after the gate runs (no actuarial, semantic, publish, reports)."""
    runner = FakeRunner(fail_on=gate)
    summary = pr.run_pipeline(pr.parse_args([]), runner, runs_dir=tmp_path)
    assert summary.status == "failed"
    names = _step_names(runner.calls)
    assert names[-1] == gate
    assert not {"actuarial", "semantic", "publish", "reports"} & set(names)


def test_resume_after_failed_gate_is_blocked(has_source, tmp_path):
    pr.run_pipeline(pr.parse_args([]), FakeRunner(fail_on="pipeline.diagnostics"), runs_dir=tmp_path)
    runner = FakeRunner()
    summary = pr.run_pipeline(pr.parse_args(["--from", "actuarial"]), runner, runs_dir=tmp_path)
    assert summary.status == "failed" and "diagnostics" in summary.error
    assert runner.calls == []
    assert summary.steps[0].status == "blocked"


def test_resume_without_any_gate_record_is_blocked(has_source, tmp_path):
    runner = FakeRunner()
    summary = pr.run_pipeline(pr.parse_args(["--only", "reports"]), runner, runs_dir=tmp_path)
    assert summary.status == "failed" and runner.calls == []


def test_from_and_only_after_passing_gates(has_source, tmp_path):
    pr.run_pipeline(pr.parse_args([]), FakeRunner(), runs_dir=tmp_path)
    runner = FakeRunner()
    pr.run_pipeline(pr.parse_args(["--from", "actuarial"]), runner, runs_dir=tmp_path)
    assert _step_names(runner.calls) == ["actuarial", "semantic", "publish", "reports"]
    runner = FakeRunner()
    pr.run_pipeline(pr.parse_args(["--only", "reports", "ingest"]), runner, runs_dir=tmp_path)
    assert _step_names(runner.calls) == ["ingest", "reports"]


def test_from_and_only_are_exclusive():
    with pytest.raises(SystemExit):
        pr.parse_args(["--from", "dbt", "--only", "reports"])


def test_semantic_skipped_until_its_module_exists(has_source, tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "OPTIONAL_MODULES", {"semantic": tmp_path / "nope" / "__main__.py"})
    runner = FakeRunner()
    summary = pr.run_pipeline(pr.parse_args([]), runner, runs_dir=tmp_path)
    assert summary.status == "ok" and "semantic" not in _step_names(runner.calls)
    assert next(s for s in summary.steps if s.name == "semantic").status == "skipped"


def test_dbt_command_uses_in_repo_profile_and_leaves_gates_to_gate_steps(has_source):
    cmd = pr.step_command("dbt", pr.parse_args([]))
    assert cmd is not None and cmd[3] == "build"
    assert cmd[cmd.index("--profiles-dir") + 1] == str(pr.DBT_DIR)
    assert cmd[cmd.index("--exclude") + 1] == "tag:gate"


def test_child_env_uses_v3_paths(monkeypatch):
    monkeypatch.setenv("SOURCE_DB_PATH", "old.db")
    for var in ("ADMIN_CORE_PATH", "LEGACY_DB_PATH", "LANDING_DIR"):
        monkeypatch.delenv(var, raising=False)
    env = pr.child_env("RUN1")
    assert "SOURCE_DB_PATH" not in env
    assert env["ADMIN_CORE_PATH"].endswith("admin_core.db")
    assert env["LEGACY_DB_PATH"] and env["LANDING_DIR"].endswith("landing")
    assert env["PIPELINE_RUN_ID"] == "RUN1" and env["DO_NOT_TRACK"] == "1"


def test_run_is_appended_to_audit_pipeline_run(has_source, tmp_path):
    wh = tmp_path / "wh.duckdb"
    duckdb.connect(str(wh)).close()
    s1 = pr.run_pipeline(pr.parse_args([]), FakeRunner(), runs_dir=tmp_path, warehouse=wh)
    s2 = pr.run_pipeline(pr.parse_args([]), FakeRunner(fail_on="pipeline.reconcile"), runs_dir=tmp_path, warehouse=wh)
    with duckdb.connect(str(wh)) as con:
        rows = con.execute("select run_id, status from audit.pipeline_run order by started_at, status").fetchall()
    assert {r[1] for r in rows} == {"ok", "failed"} and len(rows) == 2
    assert {r[0] for r in rows} == {s1.run_id, s2.run_id} or s1.run_id == s2.run_id


# --------------------------------------------------------------------------- write-audit-publish
class BuildingRunner(FakeRunner):
    """dbt writes a new core.dim_claim into the warehouse it is pointed at; each gate writes one audit row."""

    def __call__(self, argv, env) -> int:
        wh = env["WAREHOUSE_PATH"]
        if argv[2] == "dbt.cli.main" and argv[3] == "build":
            with duckdb.connect(wh) as con:
                con.execute("create schema if not exists core")
                con.execute("create or replace table core.dim_claim as select 'new' as build")
        if argv[2] in ("pipeline.diagnostics", "pipeline.reconcile"):
            with duckdb.connect(wh) as con:
                con.execute("create schema if not exists audit")
                con.execute("create table if not exists audit.diagnostics_result (run_id varchar, check_name varchar)")
                con.execute("insert into audit.diagnostics_result values (?, ?)", [env["PIPELINE_RUN_ID"], argv[2]])
        return super().__call__(argv, env)


def _live_with_old_build(tmp_path: Path) -> Path:
    wh = tmp_path / "wh.duckdb"
    with duckdb.connect(str(wh)) as con:
        con.execute("create schema core; create table core.dim_claim as select 'old' as build")
    return wh


def _build_in(db: Path) -> str:
    with duckdb.connect(str(db), read_only=True) as con:
        return con.execute("select build from core.dim_claim").fetchone()[0]


@pytest.mark.parametrize("gate", ["pipeline.diagnostics", "pipeline.reconcile"])
def test_failed_gate_leaves_the_previous_build_live(has_source, tmp_path, gate):
    """Injected gate failure: core.* in the live warehouse still holds the previous build."""
    wh = _live_with_old_build(tmp_path)
    runner = BuildingRunner(fail_on=gate)
    summary = pr.run_pipeline(pr.parse_args([]), runner, runs_dir=tmp_path / "runs", warehouse=wh)
    assert summary.status == "failed" and summary.wap == "held"
    assert _build_in(wh) == "old"
    staged = pr.staged_path(wh)
    assert _build_in(staged) == "new"  # kept for debugging, never published
    dbt_env = runner.envs[[c[2] for c in runner.calls].index("dbt.cli.main")]
    assert dbt_env["WAREHOUSE_PATH"] == str(staged)  # dbt never wrote the live file
    with duckdb.connect(str(wh), read_only=True) as con:  # the failed gate is still visible in live audit
        rows = con.execute("select check_name from audit.diagnostics_result where run_id = ?",
                           [summary.run_id]).fetchall()
    assert (gate,) in rows


def test_passing_gates_publish_the_staged_build_and_record_it(has_source, tmp_path):
    wh = _live_with_old_build(tmp_path)
    runs = tmp_path / "runs"
    summary = pr.run_pipeline(pr.parse_args([]), BuildingRunner(), runs_dir=runs, warehouse=wh)
    assert summary.status == "ok" and summary.wap == "published"
    assert _build_in(wh) == "new" and not pr.staged_path(wh).exists()
    assert summary.live_build == summary.run_id == pr.read_live_build(runs)
    with duckdb.connect(str(wh), read_only=True) as con:
        assert con.execute("select live_build from audit.pipeline_run where run_id = ?",
                           [summary.run_id]).fetchone() == (summary.run_id,)


def test_gated_steps_read_the_published_build(has_source, tmp_path):
    wh = _live_with_old_build(tmp_path)
    seen = {}

    class Peek(BuildingRunner):
        def __call__(self, argv, env) -> int:
            if argv[2] == "actuarial":
                seen["wh"], seen["build"] = env["WAREHOUSE_PATH"], _build_in(wh)
            return super().__call__(argv, env)

    pr.run_pipeline(pr.parse_args([]), Peek(), runs_dir=tmp_path / "runs", warehouse=wh)
    assert seen["build"] == "new" and seen["wh"] != str(pr.staged_path(wh))


def test_resumed_gate_publishes_the_held_build(has_source, tmp_path):
    wh = _live_with_old_build(tmp_path)
    runs = tmp_path / "runs"
    pr.run_pipeline(pr.parse_args([]), BuildingRunner(fail_on="pipeline.reconcile"), runs_dir=runs, warehouse=wh)
    assert _build_in(wh) == "old"
    runner = BuildingRunner()
    summary = pr.run_pipeline(pr.parse_args(["--from", "reconcile"]), runner, runs_dir=runs, warehouse=wh)
    assert runner.envs[0]["WAREHOUSE_PATH"] == str(pr.staged_path(wh))  # reconcile audited the held build
    assert summary.status == "ok" and summary.wap == "published" and _build_in(wh) == "new"


def test_staged_build_is_not_published_without_diagnostics_on_that_build(has_source, tmp_path):
    wh = _live_with_old_build(tmp_path)
    runs = tmp_path / "runs"
    pr.run_pipeline(pr.parse_args([]), BuildingRunner(), runs_dir=runs, warehouse=wh)  # all gates ok once
    with duckdb.connect(str(wh)) as con:
        con.execute("create or replace table core.dim_claim as select 'old' as build")
    pr.write_gate(runs, "diagnostics", "ok", "19990101T000000Z")  # diagnostics last passed before this build
    summary = pr.run_pipeline(pr.parse_args(["--only", "dbt", "reconcile"]), BuildingRunner(), runs_dir=runs,
                              warehouse=wh)
    assert summary.wap == "held" and _build_in(wh) == "old"


def test_airflow_dag_is_valid_python():
    py_compile.compile(str(DAG), doraise=True)
    ast.parse(DAG.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- the site sees its own run
def test_provisional_run_record_is_visible_to_the_reports_step(has_source, tmp_path):
    """Before reports runs, data/runs/<run_id>.json and audit.pipeline_run say 'running' with the gates ok;
    the final record then replaces the provisional row (one row per run)."""
    wh = tmp_path / "wh.duckdb"
    duckdb.connect(str(wh)).close()
    seen: dict = {}

    class Peek(FakeRunner):
        def __call__(self, argv, env) -> int:
            if argv[2] == "reports":
                run_id = env["PIPELINE_RUN_ID"]
                seen["json"] = json.loads((tmp_path / f"{run_id}.json").read_text(encoding="utf-8"))
                with duckdb.connect(str(wh)) as con:
                    seen["rows"] = con.execute("select status from audit.pipeline_run where run_id = ?",
                                               [run_id]).fetchall()
            return super().__call__(argv, env)

    summary = pr.run_pipeline(pr.parse_args([]), Peek(), runs_dir=tmp_path, warehouse=wh)
    assert seen["json"]["status"] == "running" and seen["rows"] == [("running",)]
    assert {s["name"]: s["status"] for s in seen["json"]["steps"]}["reconcile"] == "ok"
    with duckdb.connect(str(wh)) as con:
        rows = con.execute("select status from audit.pipeline_run where run_id = ?", [summary.run_id]).fetchall()
    assert rows == [("ok",)]
    assert json.loads((tmp_path / f"{summary.run_id}.json").read_text(encoding="utf-8"))["status"] == "ok"


def test_dbt_build_results_survive_the_gate_steps(has_source, tmp_path, monkeypatch):
    """The gates' dbt test overwrites target/run_results.json; the build's copy is kept per run."""
    target = tmp_path / "target"
    target.mkdir()
    monkeypatch.setenv("DBT_TARGET_PATH", str(target))

    class Dbt(FakeRunner):
        def __call__(self, argv, env) -> int:
            kind = {"dbt.cli.main": "model", "pipeline.diagnostics": "test"}.get(argv[2])
            if kind:
                (target / "run_results.json").write_text(json.dumps(
                    {"results": [{"unique_id": f"{kind}.vsc.x", "status": "success"}]}), encoding="utf-8")
            return super().__call__(argv, env)

    summary = pr.run_pipeline(pr.parse_args([]), Dbt(), runs_dir=tmp_path)
    saved = json.loads((tmp_path / summary.run_id / pr.DBT_BUILD_FILE).read_text(encoding="utf-8"))
    assert saved["results"][0]["unique_id"].startswith("model.")


# --------------------------------------------------------------------------- append-day CDC, end to end
@pytest.mark.slow
def test_append_day_cdc_loads_only_new_source_versions(tmp_path):
    """Tiny full build, then two --append-day rounds: raw growth per Admin Core table equals the source
    rows whose row_version moved past the watermark (nothing reloaded, nothing missed)."""
    import os
    import sqlite3
    import subprocess
    import sys

    env = {**os.environ, "ADMIN_CORE_PATH": str(tmp_path / "admin_core.db"),
           "LEGACY_DB_PATH": str(tmp_path / "legacy.db"), "LANDING_DIR": str(tmp_path / "landing"),
           "INTAKE_DIR": str(tmp_path / "intake"), "WAREHOUSE_PATH": str(tmp_path / "wh.duckdb"),
           "PIPELINE_RUN_ID": ""}

    def sh(*argv: str) -> None:
        subprocess.run([sys.executable, "-m", *argv], cwd=ROOT, env=env, check=True, capture_output=True)

    def raw_counts() -> dict[str, int]:
        with duckdb.connect(env["WAREHOUSE_PATH"], read_only=True) as con:
            tabs = con.execute("select table_name from information_schema.tables "
                               "where table_schema = 'raw_admin_core' and table_type = 'BASE TABLE'").fetchall()
            return {t: con.execute(f'select count(*) from raw_admin_core."{t}"').fetchone()[0] for (t,) in tabs}

    sh("generator", "--contracts", "400", "--legacy-contracts", "100")
    sh("ingest.feed")
    sh("ingest")
    for day in (1, 2):
        before = raw_counts()
        env["PIPELINE_RUN_ID"] = f"APPEND{day}"
        sh("generator", "--append-day", "1")
        sh("ingest.feed")
        sh("ingest")
        after = raw_counts()
        with duckdb.connect(env["WAREHOUSE_PATH"], read_only=True) as con:
            marks = con.execute("select table_name, watermark_from, watermark_to from audit.ingest_run "
                                "where run_id = ? and source_system = 'admin_core'", [f"APPEND{day}"]).fetchall()
        src = sqlite3.connect(env["ADMIN_CORE_PATH"])
        new_total = 0
        for table, wf, wt in marks:
            new = src.execute(f'select count(*) from "{table}" where row_version > ? and row_version <= ?',
                              [int(wf or 0), int(wt or 0)]).fetchone()[0]
            assert after.get(table, 0) - before.get(table, 0) == new, (day, table)
            new_total += new
        src.close()
        assert new_total > 0, f"append day {day} produced no new source versions"


def _stamp_seed(db: Path, seed: int) -> None:
    import sqlite3

    db.unlink(missing_ok=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE _source_epoch (epoch TEXT, seed INTEGER, as_of TEXT, built_at TEXT)")
        conn.execute("INSERT INTO _source_epoch VALUES (?, ?, '2026-10-21', '2026-10-21 00:00:00')",
                     [f"seed{seed}-abc", seed])


def test_seed_that_differs_from_the_source_regenerates(has_source, tmp_path):
    _stamp_seed(pr.ADMIN_CORE_DB, 42)
    runner = FakeRunner()
    pr.run_pipeline(pr.parse_args(["--seed", "7"]), runner, runs_dir=tmp_path / "a")
    assert runner.calls[0][2:] == ["generator", "--seed", "7"]
    same = FakeRunner()
    summary = pr.run_pipeline(pr.parse_args(["--seed", "42"]), same, runs_dir=tmp_path / "b")
    assert summary.steps[0].status == "skipped" and "generator" not in _step_names(same.calls)


def test_source_reset_in_the_run_forces_dbt_full_refresh_and_drops_snapshots(has_source, tmp_path):
    wh = tmp_path / "wh.duckdb"
    with duckdb.connect(str(wh)) as con:
        con.execute("create schema audit; create schema snapshots")
        con.execute("create table audit.source_epoch (source_system varchar, epoch varchar, seed bigint, "
                    "last_run_id varchar, reset_run_id varchar, updated_at timestamp)")
        con.execute("create table snapshots.snap_dealer as select 1 as x")

    class ResettingIngest(FakeRunner):
        def __call__(self, argv, env) -> int:
            if argv[2] == "ingest":
                with duckdb.connect(str(wh)) as con:
                    con.execute("insert into audit.source_epoch values ('admin_core', 'e', 7, ?, ?, now())",
                                [env["PIPELINE_RUN_ID"], env["PIPELINE_RUN_ID"]])
            return super().__call__(argv, env)

    runner = ResettingIngest()
    pr.run_pipeline(pr.parse_args([]), runner, runs_dir=tmp_path / "runs", warehouse=wh)
    dbt_cmd = next(c for c in runner.calls if c[2] == "dbt.cli.main")
    assert "--full-refresh" in dbt_cmd
    with duckdb.connect(str(wh)) as con:
        assert con.execute("select count(*) from information_schema.tables "
                           "where table_schema = 'snapshots'").fetchone()[0] == 0
    # A later run with no reset builds incrementally again (run ids are per second, so age the stamp).
    with duckdb.connect(str(wh)) as con:
        con.execute("update audit.source_epoch set reset_run_id = '19990101T000000Z'")
    quiet = FakeRunner()
    pr.run_pipeline(pr.parse_args([]), quiet, runs_dir=tmp_path / "runs2", warehouse=wh)
    assert "--full-refresh" not in next(c for c in quiet.calls if c[2] == "dbt.cli.main")


# --------------------------------------------------------------------------- raw is kept; freshness runs
def test_full_refresh_rebuilds_dbt_but_keeps_raw(has_source, tmp_path):
    runner = FakeRunner()
    pr.run_pipeline(pr.parse_args(["--full-refresh"]), runner, runs_dir=tmp_path / "runs")
    ingest = next(c for c in runner.calls if c[2] == "ingest")
    dbt = next(c for c in runner.calls if c[2] == "dbt.cli.main")
    assert "--full-refresh" not in ingest  # raw holds every source version: never dropped implicitly
    assert "--full-refresh" in dbt


def test_reload_raw_is_the_explicit_raw_drop_and_implies_a_dbt_full_refresh(has_source, tmp_path):
    runner = FakeRunner()
    args = pr.parse_args(["--reload-raw"])
    assert args.full_refresh
    pr.run_pipeline(args, runner, runs_dir=tmp_path / "runs")
    assert next(c for c in runner.calls if c[2] == "ingest")[-1] == "--full-refresh"
    assert "--full-refresh" in next(c for c in runner.calls if c[2] == "dbt.cli.main")


def test_source_freshness_runs_after_a_successful_dbt_build(has_source, tmp_path):
    runner = FakeRunner()
    pr.run_pipeline(pr.parse_args([]), runner, runs_dir=tmp_path / "runs")
    dbt_calls = [c for c in runner.calls if c[2] == "dbt.cli.main"]
    assert dbt_calls[0][3] == "build" and _is_freshness(dbt_calls[1])
    assert "--profiles-dir" in dbt_calls[1] and "--exclude" not in dbt_calls[1]


def test_stale_source_freshness_never_fails_the_run(has_source, tmp_path):
    class StaleRunner(FakeRunner):
        def __call__(self, argv, env) -> int:
            super().__call__(argv, env)
            return 1 if _is_freshness(list(argv)) else 0

    assert pr.run_pipeline(pr.parse_args([]), StaleRunner(), runs_dir=tmp_path / "runs").status == "ok"


# --------------------------------------------------------------------------- staging key tests from contracts
# Staging columns that differ from the contract's primary_key (renamed / normalized in staging), and
# staging models with no row-level contract (the manifest is file-level).
STAGING_KEY_RENAMES = {"stg_legacy_admin__agents": ["agent_code"], "stg_legacy_admin__claims": ["claim_number"]}
STAGING_KEY_EXTRA = {"stg_network_feed__manifest": ["feed_date", "file_name"],
                     # EAV pivot (8.4): one row per contract, not per source custom_field_id
                     "stg_admin_core__contract_custom_field": ["contract_id"]}


def _contract_staging_keys() -> dict[str, list[str]]:
    import yaml

    keys = {}
    for f in sorted((ROOT / "contracts").glob("*.yml")):
        c = yaml.safe_load(f.read_text(encoding="utf-8"))
        for table, spec in c["tables"].items():
            keys[f"stg_{c['source']}__{table}"] = list(spec["primary_key"])
    return {**keys, **STAGING_KEY_RENAMES, **STAGING_KEY_EXTRA}


def _staging_key_tests() -> dict[str, list[str]]:
    """model -> key columns tested unique (one column: unique + not_null; several: unique_combination
    plus not_null on each)."""
    import yaml

    doc = yaml.safe_load((ROOT / "dbt" / "models" / "staging" / "_staging.yml").read_text(encoding="utf-8"))
    out = {}
    for m in doc["models"]:
        cols = {c["name"]: [t if isinstance(t, str) else next(iter(t)) for t in c.get("data_tests", [])]
                for c in m.get("columns", [])}
        combo = [t["unique_combination"]["arguments"]["columns"] for t in m.get("data_tests", [])
                 if isinstance(t, dict) and "unique_combination" in t]
        if combo and all("not_null" in cols.get(k, []) for k in combo[0]):
            out[m["name"]] = list(combo[0])
        else:
            single = [k for k, tests in cols.items() if {"unique", "not_null"} <= set(tests)]
            if single:
                out[m["name"]] = single[:1]
    return out


def test_every_staging_model_has_a_primary_key_test_matching_its_contract():
    models = sorted(p.stem for p in (ROOT / "dbt" / "models" / "staging").rglob("stg_*.sql"))
    contract, tested = _contract_staging_keys(), _staging_key_tests()
    missing = [m for m in models if m not in tested]
    assert not missing, f"staging models without a primary-key test: {missing}"
    drift = {m: (tested[m], contract.get(m)) for m in models if tested[m] != contract.get(m)}
    assert not drift, f"staging key test differs from the data contract primary_key: {drift}"
