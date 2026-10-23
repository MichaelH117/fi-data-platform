"""Daily VSC platform DAG (Airflow 3.x, TaskFlow API).

    generate -> feed -> ingest -> dbt -> diagnostics -> reconcile -> actuarial -> semantic
        -> publish -> reports

The tasks come from ``pipeline/airflow/plan.py``, the same step list and commands as
``python -m pipeline.run``, so local runs, CI and Airflow execute identical code. dbt build,
diagnostics and reconcile are **quality gates**: they never retry, and when one fails every later
task ends ``upstream_failed``, so nothing is published from bad data.

Params (set them when triggering, or in ``--dag-run-conf``):

* ``target``: dbt target, ``duckdb`` (default), ``postgres`` or ``snowflake`` (dbt/profiles.yml).
  The Python steps read and write the local DuckDB file; the postgres and snowflake targets also
  need the raw layer replicated there (warehouse/snowflake/05_load_network_feed.sql and a CDC
  connector), which this repo writes but does not run.
* ``simulate_source_day``: demo mode (default on) generates one new day of source activity first.
  Turn it off for real feeds and for backfills.
* ``full_refresh``: reload raw from scratch and rebuild dbt incrementals.

Incremental by default: ingest moves forward from its own watermarks (``audit.ingest_watermark``),
so a normal run only copies rows with a newer change-tracking version or ``updated_at``. Runs are
serial (``max_active_runs=1``, ``depends_on_past`` on feed and ingest), because DuckDB allows one
writer and watermarks must advance in date order.

Backfill (Airflow 3 CLI), one run per day in date order, each processing only its own landing folder::

    airflow backfill create --dag-id vsc_daily --from-date 2026-09-01 --to-date 2026-09-30 \
        --max-active-runs 1 --reprocess-behavior failed \
        --dag-run-conf '{"simulate_source_day": false}'

Optional: set ``DBT_RENDER=cosmos`` to render dbt as one Airflow task per model and test through
astronomer-cosmos. Cosmos picks its target at parse time from ``DBT_TARGET``, not from the param.

Server-target only. Airflow is not installed in the local .venv (no Docker on the dev box), so this
file is syntax-checked by pytest (tests/test_platform.py) but has not run against a live scheduler.
Run it with ``docker compose --profile airflow up`` (the repo is mounted at /opt/airflow/repo).
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from airflow.sdk import Asset, Param, dag, task

REPO = Path(os.environ.get("VSC_REPO", "/opt/airflow/repo"))
if str(REPO) not in sys.path:  # the plan lives in the repo, next to pipeline/run.py
    sys.path.insert(0, str(REPO))

from pipeline.airflow.plan import BY_NAME, TARGETS, RunParams, bash_command, step_env  # noqa: E402

log = logging.getLogger(__name__)

DBT_TARGET = os.environ.get("DBT_TARGET", "duckdb")
DBT_RENDER = os.environ.get("DBT_RENDER", "bash")  # bash | cosmos
PYTHON = os.environ.get("VSC_PYTHON", "python")
WAREHOUSE_URI = os.environ.get("WAREHOUSE_URI", f"duckdb://{REPO}/data/warehouse/warehouse.duckdb")

# Assets let downstream DAGs (a BI refresh, a dealer e-mail) schedule on data, not on time.
ADMIN_CORE = Asset(f"sqlite://{REPO}/data/source/admin_core.db")
RAW = Asset(f"{WAREHOUSE_URI}/raw")
MARTS = Asset(f"{WAREHOUSE_URI}/marts")
GATES = Asset(f"{WAREHOUSE_URI}/audit/gates")
ACTUARIAL = Asset(f"{WAREHOUSE_URI}/actuarial")
SEMANTIC = Asset(f"file://{REPO}/semantic/model")
PUBLISH = Asset(f"file://{REPO}/site/dealers")
SITE = Asset(f"file://{REPO}/site/index.html")

EXPECTED_RUNTIME = timedelta(minutes=30)


def on_failure(context) -> None:
    """Failure hook: replace the log line with Slack/e-mail/paging in a real deployment."""
    ti = context["task_instance"]
    kind = "QUALITY GATE" if BY_NAME.get(ti.task_id) and BY_NAME[ti.task_id].gate else "task"
    log.error("VSC pipeline %s failed: %s.%s try %s (%s)", kind, ti.dag_id, ti.task_id, ti.try_number, ti.log_url)


def on_dag_success(context) -> None:
    """SLA-style check (Airflow 3 removed task SLAs): warn when the run blew its time budget."""
    run = context["dag_run"]
    if run.start_date and run.end_date and run.end_date - run.start_date > EXPECTED_RUNTIME:
        log.warning("VSC pipeline exceeded runtime budget: %s > %s", run.end_date - run.start_date, EXPECTED_RUNTIME)


def run_params(params: dict | None, ds: str | None) -> RunParams:
    p = params or {}
    return RunParams(
        target=p.get("target", DBT_TARGET),
        ds=ds,
        simulate_source_day=bool(p.get("simulate_source_day", True)),
        full_refresh=bool(p.get("full_refresh", False)),
    )


def command(step: str, params: dict | None, ds: str | None) -> str:
    return bash_command(step, run_params(params, ds), REPO, PYTHON)


def opts(step: str, **extra) -> dict:
    """Per-task settings from the plan: retries, timeout, docs, environment."""
    s = BY_NAME[step]
    return {
        "task_id": step,
        "retries": s.retries,
        "execution_timeout": timedelta(minutes=s.timeout_minutes),
        "depends_on_past": s.depends_on_past,
        "doc_md": s.doc,
        "env": step_env(REPO, DBT_TARGET),
        "append_env": True,
        **extra,
    }


DEFAULT_ARGS = {
    "owner": "platform",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=30),
    "execution_timeout": timedelta(minutes=20),
    "on_failure_callback": on_failure,
}


@dag(
    dag_id="vsc_daily",
    schedule="0 5 * * *",  # 05:00 daily, so reports are ready by the 07:00 SLA
    start_date=datetime(2026, 1, 1),
    catchup=False,  # history is loaded with `airflow backfill create`, never by accident
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    on_success_callback=on_dag_success,
    params={
        "target": Param(DBT_TARGET, type="string", enum=list(TARGETS), description="dbt target"),
        "simulate_source_day": Param(True, type="boolean", description="generate a new demo day first"),
        "full_refresh": Param(False, type="boolean", description="reload raw and rebuild incrementals"),
    },
    tags=["vsc", "warehouse", "dbt"],
    doc_md=__doc__,
)
def vsc_daily() -> None:
    @task.bash(**opts("generate"))
    def append_source_day(params=None, ds=None) -> str:
        """Demo mode only: one more day of Admin Core work plus a landing folder. Skipped otherwise."""
        return command("generate", params, ds)

    # none_failed: still runs when generate was skipped (real feeds, backfills).
    @task.bash(**opts("feed", trigger_rule="none_failed", outlets=[ADMIN_CORE]))
    def validate_feed(params=None, ds=None) -> str:
        return command("feed", params, ds)

    @task.bash(**opts("ingest", inlets=[ADMIN_CORE], outlets=[RAW]))
    def ingest_bronze(params=None, ds=None) -> str:
        """Watermark-incremental copy into raw_* (bronze)."""
        return command("ingest", params, ds)

    if DBT_RENDER == "cosmos":
        from cosmos import DbtTaskGroup, ExecutionConfig, ProfileConfig, ProjectConfig, RenderConfig
        from cosmos.constants import LoadMode, TestBehavior

        dbt = DbtTaskGroup(
            group_id="dbt",
            project_config=ProjectConfig(REPO / "dbt"),
            profile_config=ProfileConfig(
                profile_name="warranty", target_name=DBT_TARGET, profiles_yml_filepath=REPO / "dbt" / "profiles.yml"
            ),
            execution_config=ExecutionConfig(dbt_executable_path="dbt"),
            render_config=RenderConfig(load_method=LoadMode.DBT_LS, test_behavior=TestBehavior.AFTER_EACH),
            operator_args={"install_deps": True, "emit_datasets": True, "retries": 0},
            default_args={"retries": 0},
        )
    else:

        @task.bash(**opts("dbt", outlets=[MARTS]))
        def dbt_build(params=None, ds=None) -> str:
            """Gate 1: models, snapshots, seeds and every dbt test."""
            return command("dbt", params, ds)

        dbt = dbt_build()

    @task.bash(**opts("diagnostics"))
    def diagnostics_gate(params=None, ds=None) -> str:
        """Gate 2: writes audit.diagnostics_result; non-zero exit blocks everything after it."""
        return command("diagnostics", params, ds)

    @task.bash(**opts("reconcile", outlets=[GATES]))
    def reconcile_gate(params=None, ds=None) -> str:
        """Gate 3: writes audit.reconciliation; any break above $0.01 or 0 rows blocks publishing."""
        return command("reconcile", params, ds)

    @task.bash(**opts("actuarial", outlets=[ACTUARIAL]))
    def run_actuarial(params=None, ds=None) -> str:
        return command("actuarial", params, ds)

    @task.bash(**opts("semantic", outlets=[SEMANTIC]))
    def build_semantic(params=None, ds=None) -> str:
        return command("semantic", params, ds)

    @task.bash(**opts("publish", inlets=[MARTS], outlets=[PUBLISH]))
    def publish_benchmarks(params=None, ds=None) -> str:
        """Aggregate data products and per-dealer packets."""
        return command("publish", params, ds)

    @task.bash(**opts("reports", outlets=[SITE]))
    def build_reports(params=None, ds=None) -> str:
        return command("reports", params, ds)

    (
        append_source_day()
        >> validate_feed()
        >> ingest_bronze()
        >> dbt
        >> diagnostics_gate()
        >> reconcile_gate()
        >> run_actuarial()
        >> build_semantic()
        >> publish_benchmarks()
        >> build_reports()
    )


vsc_daily()
