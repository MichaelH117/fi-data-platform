"""Platform portability checks: Snowflake/Postgres SQL, dbt profiles, the Airflow DAG and CI.

None of these files is executed locally (no servers, no paid accounts, no Airflow in the .venv), so
the tests check what can be checked offline: the SQL splits into well-formed statements and creates
the objects the design names, the YAML parses, and the DAG's task order matches pipeline.run.
"""

from __future__ import annotations

import ast
import json
import os
import py_compile
import re
import shlex
import sys
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd
import pytest
import yaml

from pipeline import envs
from pipeline import run as pr
from pipeline.airflow import plan
from publish import packets as pk

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import env_diff  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SNOWFLAKE = ROOT / "warehouse" / "snowflake"
POSTGRES_RLS = ROOT / "warehouse" / "postgres" / "rls.sql"
DAG = ROOT / "pipeline" / "airflow" / "dags" / "vsc_daily.py"
CI = ROOT / ".github" / "workflows" / "ci.yml"
PROFILES = ROOT / "dbt" / "profiles.yml"

# docs/V3_DESIGN.md section 1, the binding step order.
DESIGN_STEPS = ("generate", "feed", "ingest", "dbt", "diagnostics", "reconcile", "actuarial", "semantic",
                "publish", "reports")
SNOWFLAKE_VERBS = {"USE", "CREATE", "ALTER", "GRANT", "COPY", "UPDATE", "INSERT", "SELECT"}
POSTGRES_VERBS = {"DO", "CREATE", "ALTER", "GRANT", "REVOKE", "COMMENT"}


# ------------------------------------------------------------------------------------------------
# A small SQL splitter: strips -- comments, respects '...' strings and $$...$$ bodies.
# ------------------------------------------------------------------------------------------------
def split_sql(text: str) -> list[str]:
    statements, buf, i, n = [], [], 0, len(text)
    in_str = in_dollar = False
    while i < n:
        ch = text[i]
        if in_str:
            buf.append(ch)
            if ch == "'":
                if text[i + 1 : i + 2] == "'":  # escaped quote
                    buf.append("'")
                    i += 1
                else:
                    in_str = False
        elif in_dollar:
            buf.append(ch)
            if text.startswith("$$", i):
                buf.append("$")
                i += 1
                in_dollar = False
        elif text.startswith("--", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif ch == "'":
            in_str = True
            buf.append(ch)
        elif text.startswith("$$", i):
            in_dollar = True
            buf.append("$$")
            i += 1
        elif ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
        else:
            buf.append(ch)
        i += 1
    assert not in_str and not in_dollar, "unterminated string or $$ body"
    tail = "".join(buf).strip()
    assert not tail, f"statement without a terminating semicolon: {tail[:80]!r}"
    return statements


def balanced(stmt: str) -> bool:
    depth = 0
    for ch in re.sub(r"'(?:[^']|'')*'", "''", stmt):
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if depth < 0:
            return False
    return depth == 0


def snowflake_sql() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(SNOWFLAKE.glob("*.sql")))


def test_split_sql_helper():
    assert split_sql("-- c;\nSELECT ';' ; CREATE FUNCTION f() AS $$ a; b $$;") == [
        "SELECT ';'",
        "CREATE FUNCTION f() AS $$ a; b $$",
    ]


@pytest.mark.parametrize("path", sorted(SNOWFLAKE.glob("*.sql")), ids=lambda p: p.name)
def test_snowflake_files_are_well_formed(path: Path):
    stmts = split_sql(path.read_text(encoding="utf-8"))
    assert stmts, f"{path.name} has no statements"
    for s in stmts:
        assert s.split()[0].upper() in SNOWFLAKE_VERBS, f"{path.name}: unexpected statement {s[:60]!r}"
        assert balanced(s), f"{path.name}: unbalanced parentheses in {s[:60]!r}"


def test_snowflake_files_are_numbered_in_run_order():
    names = [p.name for p in sorted(SNOWFLAKE.glob("*.sql"))]
    assert [n[:2] for n in names] == [f"{i:02d}" for i in range(1, len(names) + 1)]
    assert (SNOWFLAKE / "README.md").exists()


def test_snowflake_objects_match_design():
    sql = re.sub(r"[ \t]+", " ", re.sub(r"--[^\n]*", "", snowflake_sql())).upper()
    for db in ("RAW", "ANALYTICS"):
        assert re.search(rf"CREATE DATABASE IF NOT EXISTS {db}\b", sql)
    for schema in ("RAW.ADMIN_CORE", "RAW.LEGACY_ADMIN", "RAW.NETWORK_FEED", "RAW.AUDIT", "ANALYTICS.STAGING",
                   "ANALYTICS.INTERMEDIATE", "ANALYTICS.SEEDS", "ANALYTICS.SNAPSHOTS", "ANALYTICS.CORE",
                   "ANALYTICS.FINANCE", "ANALYTICS.CLAIMS", "ANALYTICS.DEALER", "ANALYTICS.REGULATORY"):
        assert f"CREATE SCHEMA IF NOT EXISTS {schema}" in sql, schema
    for wh in ("LOADING", "TRANSFORMING", "REPORTING"):
        block = re.search(rf"CREATE WAREHOUSE IF NOT EXISTS {wh} WITH(.*?);", sql, re.S)
        assert block, wh
        assert "WAREHOUSE_SIZE = 'XSMALL'" in block.group(1)
        assert "AUTO_RESUME = TRUE" in block.group(1)
        suspend = int(re.search(r"AUTO_SUSPEND = (\d+)", block.group(1)).group(1))
        assert 0 < suspend <= 600, f"{wh} auto-suspend {suspend}s"
        assert f"ALTER WAREHOUSE {wh} SET RESOURCE_MONITOR" in sql
    for role in ("LOADER", "TRANSFORMER", "REPORTER"):
        assert f"CREATE ROLE IF NOT EXISTS {role}" in sql
    monitors = re.findall(r"CREATE RESOURCE MONITOR IF NOT EXISTS \w+ WITH(.*?);", sql, re.S)
    assert len(monitors) >= 3
    for m in monitors:
        assert "ON 80 PERCENT DO NOTIFY" in m and "ON 100 PERCENT DO SUSPEND" in m
    assert "CREATE ROW ACCESS POLICY IF NOT EXISTS ANALYTICS.SECURITY.RAP_TENANT_DEALER" in sql
    assert "ADD ROW ACCESS POLICY" in sql
    assert "CREATE STREAM" in sql and "CREATE TASK" in sql
    assert re.search(r"CLONE ANALYTICS\b", sql)


def test_reporter_never_reads_raw_or_staging():
    grants = [s for s in split_sql(snowflake_sql()) if s.upper().startswith("GRANT") and "REPORTER" in s.upper()]
    assert grants
    for g in grants:
        target = g.upper()
        assert "DATABASE RAW" not in target and "RAW." not in target, g
        assert "STAGING" not in target and "INTERMEDIATE" not in target, g


def test_no_secrets_in_platform_files():
    texts = [snowflake_sql(), POSTGRES_RLS.read_text(encoding="utf-8"), PROFILES.read_text(encoding="utf-8")]
    for t in texts:
        assert not re.search(r"(?i)\bpassword\s*=\s*'[^']+'", t)
        assert "BEGIN PRIVATE KEY" not in t
        assert not re.search(r"RSA_PUBLIC_KEY\s*=\s*'MII", t)


def test_postgres_rls_is_well_formed():
    stmts = split_sql(POSTGRES_RLS.read_text(encoding="utf-8"))
    for s in stmts:
        assert s.split()[0].upper() in POSTGRES_VERBS, s[:60]
        assert balanced(s), s[:60]
    text = "\n".join(stmts)
    assert "ENABLE ROW LEVEL SECURITY" in text
    assert "CREATE POLICY" in text and "FOR SELECT TO reporter" in text
    assert "security.user_dealer_access" in text
    assert "SECURITY DEFINER" in text and "SET search_path" in text


# ------------------------------------------------------------------------------------------------
# dbt profiles
# ------------------------------------------------------------------------------------------------
def test_profiles_have_all_targets_and_no_literal_credentials():
    prof = yaml.safe_load(PROFILES.read_text(encoding="utf-8"))["warranty"]
    assert prof["target"] == "duckdb"
    outs = prof["outputs"]
    assert set(plan.TARGETS) <= set(outs)
    assert "env_var('WAREHOUSE_PATH'" in outs["duckdb"]["path"]
    assert outs["snowflake"]["type"] == "snowflake"
    for key in ("account", "user", "private_key_path", "role", "warehouse", "database"):
        assert "env_var(" in str(outs["snowflake"][key]), key
    assert "env_var(" in outs["postgres"]["password"]
    assert "password" not in outs["snowflake"]


# ------------------------------------------------------------------------------------------------
# Airflow plan + DAG
# ------------------------------------------------------------------------------------------------
def test_plan_follows_design_order():
    assert plan.STEP_NAMES == DESIGN_STEPS


def test_plan_matches_pipeline_run_steps():
    """Every pipeline.run step is in the DAG, in the same relative order."""
    assert set(pr.STEPS) <= set(plan.STEP_NAMES)
    assert [s for s in plan.STEP_NAMES if s in pr.STEPS] == list(pr.STEPS)


def test_gates_never_retry_and_come_before_outputs():
    gates = [s for s in plan.STEPS if s.gate]
    assert [g.name for g in gates] == ["dbt", "diagnostics", "reconcile"]
    assert all(g.retries == 0 for g in gates)
    last_gate = max(plan.STEP_NAMES.index(g.name) for g in gates)
    for out in ("publish", "reports", "semantic", "actuarial"):
        assert plan.STEP_NAMES.index(out) > last_gate


def test_plan_commands_demo_mode():
    p = plan.RunParams()
    assert plan.argv("generate", p) == ["python", "-m", "generator", "--append-day", "1"]
    assert plan.argv("feed", p) == ["python", "-m", "ingest.feed"]
    assert plan.argv("ingest", p) == ["python", "-m", "ingest"]
    dbt = plan.argv("dbt", p)
    assert dbt[3] == "build" and dbt[dbt.index("--target") + 1] == "duckdb"
    assert dbt[dbt.index("--profiles-dir") + 1] == "dbt" and "--vars" not in dbt


@pytest.mark.parametrize("target", plan.TARGETS)
def test_plan_target_is_a_parameter(target):
    cmd = plan.argv("dbt", plan.RunParams(target=target))
    assert cmd[cmd.index("--target") + 1] == target


def test_plan_rejects_unknown_target():
    with pytest.raises(ValueError):
        plan.RunParams(target="bigquery")


def test_plan_backfill_processes_its_own_day():
    p = plan.RunParams(ds="2026-09-15", simulate_source_day=False, full_refresh=True)
    assert plan.argv("generate", p) is None
    assert plan.argv("feed", p) == ["python", "-m", "ingest.feed", "--date", "2026-09-15"]
    assert plan.argv("ingest", p)[-1] == "--full-refresh"
    dbt = plan.argv("dbt", p)
    assert "--full-refresh" in dbt
    assert json.loads(dbt[dbt.index("--vars") + 1]) == {"as_of_date": "2026-09-15"}


def test_bash_command_skips_and_quotes():
    skip = plan.bash_command("generate", plan.RunParams(simulate_source_day=False), "/repo")
    assert skip.endswith(f"exit {plan.SKIP_EXIT_CODE}")
    line = plan.bash_command("dbt", plan.RunParams(ds="2026-09-15", simulate_source_day=False), "/opt/my repo")
    assert line.startswith("cd '/opt/my repo' && ")
    assert shlex.split(line.split(" && ", 1)[1]) == plan.argv("dbt", plan.RunParams(ds="2026-09-15",
                                                                                     simulate_source_day=False))


def test_step_env_uses_design_variables():
    env = plan.step_env("/repo", "snowflake")
    assert {"ADMIN_CORE_PATH", "LEGACY_DB_PATH", "LANDING_DIR", "WAREHOUSE_PATH"} <= set(env)
    assert env["DBT_TARGET"] == "snowflake" and "SOURCE_DB_PATH" not in env


def _dag_tree() -> ast.Module:
    py_compile.compile(str(DAG), doraise=True)
    return ast.parse(DAG.read_text(encoding="utf-8"))


def test_dag_has_one_task_per_step_in_order():
    tree = _dag_tree()
    task_ids = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "opts" and node.args:
            task_ids.append((node.lineno, node.args[0].value))
    assert [t for _, t in sorted(task_ids)] == list(plan.STEP_NAMES)
    chain = next(n for n in ast.walk(tree) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.RShift))
    called = [c.func.id if isinstance(c, ast.Call) else c.id for c in _flatten_rshift(chain)]
    assert called == ["append_source_day", "validate_feed", "ingest_bronze", "dbt", "diagnostics_gate",
                      "reconcile_gate", "run_actuarial", "build_semantic", "publish_benchmarks", "build_reports"]


def _flatten_rshift(node):
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.RShift):
        return [*_flatten_rshift(node.left), *_flatten_rshift(node.right)]
    return [node]


def test_dag_settings():
    src = DAG.read_text(encoding="utf-8")
    assert "max_active_runs=1" in src and "catchup=False" in src
    assert '"target": Param(' in src and "enum=list(TARGETS)" in src
    assert 'trigger_rule="none_failed"' in src  # feed still runs when demo generation is skipped
    assert "airflow backfill create" in src


class _FakeTask:
    """Stands in for an Airflow task: records its kwargs, renders its command, chains with >>."""

    order: list[_FakeTask] = []

    def __init__(self, fn, kwargs):
        self.fn, self.kwargs, self.task_id = fn, kwargs, kwargs["task_id"]
        self.downstream: list[_FakeTask] = []
        _FakeTask.order.append(self)

    def __rshift__(self, other):
        self.downstream.append(other)
        return other


def _fake_airflow(monkeypatch):
    import sys
    import types

    def bash(**kwargs):
        return lambda fn: (lambda: _FakeTask(fn, kwargs))

    def dag(**dag_kwargs):
        def wrap(fn):
            def build():
                fn()
                return dag_kwargs
            return build
        return wrap

    sdk = types.ModuleType("airflow.sdk")
    sdk.Asset = lambda uri: ("asset", uri)
    sdk.Param = lambda default, **kw: {"default": default, **kw}
    sdk.dag = dag
    sdk.task = types.SimpleNamespace(bash=bash)
    monkeypatch.setitem(sys.modules, "airflow", types.ModuleType("airflow"))
    monkeypatch.setitem(sys.modules, "airflow.sdk", sdk)
    monkeypatch.setenv("VSC_REPO", str(ROOT))
    monkeypatch.delenv("DBT_RENDER", raising=False)


def test_dag_executes_with_stub_airflow(monkeypatch):
    """Execute the DAG module against a stub airflow.sdk: tasks, chain, settings and rendered commands."""
    import importlib.util

    _fake_airflow(monkeypatch)
    _FakeTask.order = []
    spec = importlib.util.spec_from_file_location("vsc_daily_under_test", DAG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    tasks = {t.task_id: t for t in _FakeTask.order}
    assert sorted(tasks) == sorted(plan.STEP_NAMES) and len(_FakeTask.order) == len(plan.STEPS)
    node, chain = tasks["generate"], ["generate"]
    while node.downstream:
        node = node.downstream[0]
        chain.append(node.task_id)
    assert chain == list(plan.STEP_NAMES)
    for name, t in tasks.items():
        step = plan.BY_NAME[name]
        assert t.kwargs["retries"] == step.retries
        assert t.kwargs["env"]["WAREHOUSE_PATH"].endswith("warehouse.duckdb")
    assert tasks["feed"].kwargs["trigger_rule"] == "none_failed"

    backfill = {"target": "snowflake", "simulate_source_day": False, "full_refresh": False}
    assert tasks["generate"].fn(params=backfill, ds="2026-09-15").endswith("exit 99")
    assert "--date 2026-09-15" in tasks["feed"].fn(params=backfill, ds="2026-09-15")
    assert "--target snowflake" in tasks["dbt"].fn(params=backfill, ds="2026-09-15")
    assert tasks["ingest"].fn(params=None, ds=None).endswith("-m ingest")


def test_dag_imports_under_airflow():
    """Runs only where Airflow 3 is installed (the Docker image); skipped in the local .venv."""
    pytest.importorskip("airflow.sdk")
    from airflow.models.dagbag import DagBag

    bag = DagBag(dag_folder=str(DAG), include_examples=False)
    assert not bag.import_errors, bag.import_errors
    dag = bag.get_dag("vsc_daily")
    assert [t for t in dag.topological_sort() if t.task_id in plan.STEP_NAMES]


# ------------------------------------------------------------------------------------------------
# CI and the task runners
# ------------------------------------------------------------------------------------------------
def test_ci_workflow_runs_lint_tests_and_dbt_on_duckdb():
    wf = yaml.safe_load(CI.read_text(encoding="utf-8"))
    runs = "\n".join(step.get("run", "") for job in wf["jobs"].values() for step in job["steps"])
    assert "ruff check ." in runs
    assert "pytest -q" in runs
    assert re.search(r"dbt\.cli\.main build .*--target duckdb", runs)
    for job in wf["jobs"].values():
        assert job.get("timeout-minutes"), "every CI job needs a timeout"


def test_task_runners_agree():
    make = set(re.findall(r"^([a-z][\w-]*):", (ROOT / "Makefile").read_text(encoding="utf-8"), re.M))
    ps1 = (ROOT / "tasks.ps1").read_text(encoding="utf-8")
    ps_tasks = set(re.search(r'ValidateSet\(([^)]*)\)', ps1).group(1).replace('"', "").replace(" ", "").split(","))
    assert make <= ps_tasks


def test_compose_files_parse_and_mount_rls():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    vols = compose["services"]["warehouse"]["volumes"]
    assert any("warehouse/postgres/rls.sql" in v for v in vols)
    env = compose["x-airflow"]["environment"]
    assert {"PGHOST", "PGUSER", "PGPASSWORD", "PGDATABASE"} <= set(env)


# --------------------------------------------------------------------------- dev / UAT / prod environments
def test_environments_never_share_a_path(tmp_path):
    paths = {name: envs.env_paths(name, tmp_path) for name in envs.ENVS}
    for field in ("warehouse", "dbt_target", "publish", "site", "runs", "admin_core", "landing"):
        values = [str(getattr(p, field)) for p in paths.values()]
        assert len(set(values)) == len(values), field
    assert paths["prod"].warehouse == tmp_path / "data" / "warehouse" / "warehouse.duckdb"
    assert paths["uat"].dbt_target == "target_uat"  # relative to dbt/ (V3_DESIGN 4.2 amendment)
    with pytest.raises(ValueError):
        envs.env_paths("qa", tmp_path)


def test_activate_seeds_sources_from_prod_and_sets_every_variable(tmp_path):
    prod = envs.env_paths("prod", tmp_path)
    prod.admin_core.parent.mkdir(parents=True)
    prod.admin_core.write_bytes(b"core")
    prod.legacy.write_bytes(b"legacy")
    (prod.landing / "2026-10-01").mkdir(parents=True)
    environ = {"WAREHOUSE_PATH": "stray.duckdb"}
    uat = envs.activate("uat", environ, tmp_path)
    assert environ["WAREHOUSE_PATH"] == str(uat.warehouse) and environ["PIPELINE_ENV"] == "uat"
    assert environ["SITE_DIR"] == str(uat.site) and environ["PACKETS_DIR"] == str(uat.publish / "dealers")
    assert uat.admin_core.read_bytes() == b"core" and (uat.landing / "2026-10-01").is_dir()
    uat.admin_core.write_bytes(b"changed in uat")  # a second activation never overwrites the env's copy
    envs.activate("uat", environ, tmp_path)
    assert uat.admin_core.read_bytes() == b"changed in uat" and prod.admin_core.read_bytes() == b"core"


def test_pipeline_run_env_flag_uses_the_env_runs_dir(monkeypatch, tmp_path):
    assert pr.parse_args(["--env", "uat"]).env == "uat"
    with pytest.raises(SystemExit):
        pr.parse_args(["--env", "qa"])
    seen = {}
    monkeypatch.setattr(pr, "activate_env", lambda name: envs.env_paths(name, tmp_path))
    monkeypatch.setattr(pr, "_setup_logging", lambda d: None)
    monkeypatch.setattr(pr, "ADMIN_CORE_DB", pr.ADMIN_CORE_DB)  # restored after the test

    def fake_run(args, runs_dir):
        seen["runs"] = runs_dir
        return pr.RunSummary("x", "now", status="ok")

    monkeypatch.setattr(pr, "run_pipeline", fake_run)
    assert pr.main(["--env", "uat", "--only", "publish"]) == 0
    assert seen["runs"] == tmp_path / "data" / "env" / "uat" / "runs"
    assert pr.ADMIN_CORE_DB == tmp_path / "data" / "env" / "uat" / "source" / "admin_core.db"


def _mini_warehouse(path, paid: float, rows: int = 2):
    with duckdb.connect(str(path)) as con:
        con.execute("create schema core; create schema finance")
        con.execute("create table core.fct_claim (claim_key varchar, paid_amount double, loss_ratio double, n bigint)")
        for i in range(rows):
            con.execute("insert into core.fct_claim values (?, ?, 0.5, 3)", [str(i), paid / rows])
        con.execute("create table finance.mart_fee (late_fee double)")
        con.execute("insert into finance.mart_fee values (10.0)")
        con.execute("create table core.helper_not_a_model (amount double)")


def test_env_diff_passes_explains_and_fails(tmp_path):
    base, cand = tmp_path / "prod.duckdb", tmp_path / "uat.duckdb"
    _mini_warehouse(base, 100.0)
    _mini_warehouse(cand, 100.0)
    out = tmp_path / "env_diff.json"
    args = ["--base-warehouse", str(base), "--candidate-warehouse", str(cand), "--out", str(out)]
    assert env_diff.main(args) == 0
    res = json.loads(out.read_text(encoding="utf-8"))
    assert res["status"] == "pass" and res["tables"] == 2 and res["counts"]["fail"] == 0
    assert set(env_diff.profile(base)["core.fct_claim"]) == {"rows", "paid_amount"}  # ratios, counts: not money

    cand.unlink()
    _mini_warehouse(cand, 150.0, rows=3)  # one more row and $50 more paid
    assert env_diff.main(args) == 1
    res = json.loads(out.read_text(encoding="utf-8"))
    assert {(d["measure"], d["status"]) for d in res["differences"]} == {("rows", "fail"), ("paid_amount", "fail")}

    expect = tmp_path / "expected.csv"
    expect.write_text("table,measure,reason\ncore.fct_claim,*,new claim status in scope (ticket 42)\n",
                      encoding="utf-8")
    assert env_diff.main([*args, "--expect", str(expect)]) == 0
    res = json.loads(out.read_text(encoding="utf-8"))
    assert res["counts"]["explained"] == 2 and res["differences"][0]["reason"].startswith("new claim")
    missing = ["--base-warehouse", str(tmp_path / "missing.duckdb"), "--candidate-warehouse", str(cand),
               "--out", str(out)]
    assert env_diff.main(missing) == 2


def test_ops_page_shows_the_latest_env_diff(tmp_path):
    from reports import ops

    assert ops.env_diff_section(tmp_path / "none.json") is None
    rows = [{"table": "core.fct_claim", "measure": "paid_amount", "base": 100.0, "candidate": 150.0, "diff": 50.0,
             "status": "fail", "reason": ""}]
    (tmp_path / "d.json").write_text(json.dumps(env_diff.summarize(rows, "prod", "uat", Path("a"), Path("b"))),
                                     encoding="utf-8")
    sec = ops.env_diff_section(tmp_path / "d.json")
    assert sec.title == "Promotion check: UAT vs PROD" and "1 unexplained" in sec.caption
    assert sec.rows[0][0] == "core.fct_claim" and sec.rows[0][5]["status"] == "critical"


def test_release_doc_names_the_promotion_check():
    text = (ROOT / "docs" / "operating-model" / "RELEASE.md").read_text(encoding="utf-8")
    assert "--env uat" in text and "tools\\env_diff.py" in text and "Promotion check" in text


# --------------------------------------------------------------------------- dealer and program packets
WAREHOUSE = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))


def test_packet_formats_and_period_labels():
    assert pk.fmt_value(1.0, "int") == "1" and pk.fmt_value(1209.894, "money") == "$1,209.89"
    assert pk.fmt_value(0.4205, "pct") == "42.0%" and pk.fmt_value(float("nan"), "money") == "-"
    # sign before the symbol, and no negative zero ($-0.00 / -0.0%) when a tiny negative rounds away
    assert pk.fmt_value(-5, "money") == "-$5.00" and pk.fmt_value(-0.004, "money") == "$0.00"
    assert pk.fmt_value(-0.0001, "pct") == "0.0%" and pk.fmt_value(-0.02, "pct") == "-2.0%"
    assert pk.fmt_value(-0.00001, "ratio") == "0.0000"
    per = pk.Periods(date(2026, 10, 21), date(2025, 10, 1), date(2026, 9, 1))
    assert per.t12_label == "T12 to Sep 2026" and (per.p_start, per.p_end) == (202510, 202609)
    assert per.label("t12").startswith("T12 to Sep 2026") and per.label("asof") == "As of 2026-10-21"
    for spec in pk.TABLE_SPECS.values():  # every packet table reads a certified mart, never a fact
        assert "fct_" not in spec.sql and "dealer.mart_" in spec.sql
        assert all(c.label and "_" not in c.label for c in spec.cols)
    assert "claims.mart_claims_kpi" in pk.PROGRAM_CLAIMS.sql


@pytest.fixture(scope="module")
def wh():
    if not WAREHOUSE.exists():
        pytest.skip("warehouse not built")
    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        try:
            con.execute("select 1 from dealer.mart_dealer_scorecard limit 1")
        except duckdb.CatalogException:
            pytest.skip("dealer marts not built")
        yield con


@pytest.fixture(scope="module")
def built(wh, tmp_path_factory):
    out = tmp_path_factory.mktemp("packets")
    pk.build_packets(wh, out)
    return out


def test_packet_totals_equal_mart_totals(wh):
    per = pk.periods(wh)
    sc = ("select coalesce(sum({c}), 0) from dealer.mart_dealer_scorecard where dealer_key = ? and tenant_key = ? "
          "and month_start between ? and ?")
    for p in pk.packets(wh)[::20]:
        t = pk.packet_tables(wh, p, per)
        key = [p.dealer_key, p.tenant_key]
        written = wh.execute(sc.format(c="contracts_written"), [*key, per.t12_start, per.t12_end]).fetchone()[0]
        assert t["production"]["contracts_written"].sum() == written
        if len(t["kpi"]):
            k = t["kpi"].iloc[0]
            fees = wh.execute(sc.format(c="late_fees"), [*key, per.t12_start, per.t12_end]).fetchone()[0]
            assert k["contracts_written"] == written and abs(k["late_fees"] - fees) < 0.01
            last = wh.execute("select t12_contracts_written, t12_late_fees from dealer.mart_dealer_scorecard "
                              "where dealer_key = ? and tenant_key = ? and month_start = ?",
                              [*key, per.t12_end]).fetchone()
            if last:  # the mart's own T12 columns agree with the packet's T12 sums
                assert k["contracts_written"] == last[0] and abs(k["late_fees"] - last[1]) < 0.01
        aging = wh.execute(
            "select coalesce(sum(cash_transactions), 0), coalesce(sum(late_fees), 0) from dealer.mart_remittance_aging "
            "where dealer_key = ? and tenant_key = ? and posting_period_key between ? and ?",
            [*key, per.p_start, per.p_end]).fetchone()
        assert t["remittance_aging"]["t12_remittances"].sum() == aging[0]
        assert abs(t["remittance_aging"]["t12_late_fees"].sum() - aging[1]) < 0.01
        bands = {r[0] for r in wh.execute("select aging_bucket from seeds.remit_aging_buckets").fetchall()}
        assert set(t["remittance_aging"]["age_band"]) <= bands  # the shared seed bands only
        pending = wh.execute("select count(*) from dealer.mart_dealer_pending_contracts where dealer_key = ? "
                             "and tenant_key = ?", key).fetchone()[0]
        assert len(t["pending_contracts"]) == pending
        assert not {"customer_first_name", "customer_last_name", "vin_token"} & set(t["pending_contracts"])


def test_packet_html_and_xlsx_are_readable_and_labelled(built, wh):
    p = pk.packets(wh)[0]
    d = built / p.tenant_code / p.dealer_number
    page = (d / "index.html").read_text(encoding="utf-8")
    per = pk.periods(wh)
    assert per.t12_label in page and "Headline KPIs" in page and "Loss ratio" in page
    assert "entered_month" not in page and "coverage_cost" not in page and "Coverage cost" in page
    openpyxl = pytest.importorskip("openpyxl")
    ws = openpyxl.load_workbook(d / "packet.xlsx")["production"]
    assert ws["A2"].value.startswith(per.t12_label) and ws["B4"].value == "Contracts written"
    if ws.max_row >= 5:
        assert ws["B5"].number_format == "#,##0" and ws["E5"].number_format.startswith('"$"')


def test_program_packet_holds_only_its_dealers_and_ties_to_them(built, wh):
    per = pk.periods(wh)
    by_tenant: dict[str, list] = {}
    for p in pk.packets(wh):
        by_tenant.setdefault(p.tenant_code, []).append(p)
    for code, members in by_tenant.items():
        d = built / code
        meta = json.loads((d / "program.json").read_text(encoding="utf-8"))
        assert sorted(meta["dealers"]) == sorted(p.dealer_number for p in members)
        page = (d / "index.html").read_text(encoding="utf-8")
        assert members[0].reportable_name in page
        others = {p.dealer_number for c, ms in by_tenant.items() if c != code for p in ms} - set(meta["dealers"])
        assert not any(f"href='{n}/index.html'" in page for n in others)
        assert all((d / n / "index.html").exists() for n in meta["dealers"])
        total = pd.read_csv(d / "program_program_kpi.csv").iloc[0]
        dealer_sum = dict.fromkeys(("contracts_written", "late_fees", "incurred_claims", "earned_claims_reserve"), 0.0)
        for p in members:
            kpi = pd.read_csv(d / p.dealer_number / "kpi.csv")
            for k in dealer_sum:
                dealer_sum[k] += float(kpi[k].sum()) if len(kpi) else 0.0
        for k, v in dealer_sum.items():
            assert abs(total[k] - v) < 0.01, (code, k)
        mart = wh.execute(
            "select sum(contracts_written), sum(late_fees) from dealer.mart_dealer_scorecard s "
            "join core.dim_tenant t using (tenant_key) where t.tenant_code = ? and month_start between ? and ? "
            "and (s.tenant_key, s.dealer_key) in (select tenant_key, dealer_key from core.dim_user_dealer_access)",
            [code, per.t12_start, per.t12_end]).fetchone()
        assert total["contracts_written"] == mart[0] and abs(total["late_fees"] - mart[1]) < 0.01
        assert (d / "program.xlsx").exists() == meta["xlsx"]


# --- demo run history (tools/demo_history.py; tasks.ps1 demo / make demo) ----------------------------------

import demo_history as dh  # noqa: E402


def test_demo_plan_injects_one_gate_failure_and_recovers():
    steps = dh.plan(days=3, fail_day=2)
    assert steps[0].argv == ("-m", "pipeline.run")
    failing = [s for s in steps if not s.expect_ok]
    assert len(failing) == 1 and failing[0].fail_step == "reconcile"
    i = steps.index(failing[0])
    extract, late, recovery = steps[i - 2], steps[i - 1], steps[i + 1]
    # the day's extract lands, then the source writes after the cutoff, then the gates run
    assert pr.select_steps(pr.parse_args(list(extract.argv[2:])).only, None) == ["generate", "feed", "ingest"]
    assert late.argv == ("-m", "generator", "--append-day", "1")
    gates_run = pr.parse_args(list(failing[0].argv[2:]))
    ran = pr.select_steps(gates_run.only, gates_run.start)
    assert "ingest" not in ran and {"diagnostics", "reconcile", "publish", "reports"} <= set(ran)
    assert gates_run.append_day == 0  # no new source day: the break is the late batch only
    rec = pr.parse_args(list(recovery.argv[2:]))
    assert {"feed", "ingest", "reconcile", "reports"} <= set(pr.select_steps(rec.only, rec.start))
    assert sum(1 for s in steps if s.label.startswith("day ")) == 2 + 4  # 2 normal days + the incident day


def test_demo_plan_every_pipeline_command_parses():
    for s in dh.plan(days=4, fail_day=4, build=False):
        if s.is_pipeline_run:
            pr.parse_args(list(s.argv[2:]))  # exits on a bad flag
    assert dh.plan(days=2, fail_day=0)[-1].argv == ("-m", "pipeline.run", "--append-day")
    with pytest.raises(ValueError):
        dh.plan(days=2, fail_day=3)


def test_demo_execute_stops_on_unexpected_outcomes():
    steps = dh.plan(days=2, fail_day=1, build=False)
    expected = [0 if s.expect_ok else 1 for s in steps]
    calls: list[tuple[str, ...]] = []

    def runner(codes):
        it = iter(codes)
        return lambda argv: (calls.append(tuple(argv)), next(it))[1]

    out: list[str] = []
    assert dh.execute(steps, runner(expected), out.append, failed_step=lambda: "reconcile") == 0
    assert len(calls) == len(steps)
    # the injected failure must be the reconcile gate, not a dbt error
    assert dh.execute(steps, runner(expected), out.append, failed_step=lambda: "dbt") == 1
    # a gate that unexpectedly passes stops the demo
    calls.clear()
    assert dh.execute(steps, runner([0] * len(steps)), out.append, failed_step=lambda: None) == 1
    assert len(calls) == 3


def test_demo_last_failed_step_reads_newest_summary(tmp_path):
    (tmp_path / "20260101T000000Z.json").write_text(json.dumps({"steps": [{"name": "dbt", "status": "failed"}]}))
    (tmp_path / "20260102T000000Z.json").write_text(json.dumps(
        {"steps": [{"name": "diagnostics", "status": "ok"}, {"name": "reconcile", "status": "failed"}]}))
    assert dh.last_failed_step(tmp_path) == "reconcile"
    assert dh.last_failed_step(tmp_path / "missing") is None


def test_demo_and_schedule_targets_in_both_runners():
    make = (ROOT / "Makefile").read_text(encoding="utf-8")
    ps1 = (ROOT / "tasks.ps1").read_text(encoding="utf-8")
    for target in ("demo", "schedule", "unschedule"):
        assert re.search(rf"^{target}:", make, re.M) and f'"{target}"' in ps1
    assert "tools/demo_history.py" in make and "tools\\demo_history.py" in ps1
    # the scheduled run lands before the 07:00 SLO deadline (reports/ops.py)
    assert "-At 6am" in ps1 and "0 6 * * 1-5" in make
