"""Airflow task plan: the steps of ``python -m pipeline.run`` as plain data.

The DAG in ``dags/vsc_daily.py`` builds one task per step from this module. It has no Airflow
imports, so pytest and CI can check the order, the gates and the exact commands without Airflow
installed (Airflow is server-only; it is not in the local .venv).

Rules the plan encodes:

* **Order** matches ``pipeline.run.STEPS`` and docs/V3_DESIGN.md section 1.
* **Gates** (dbt build, diagnostics, reconcile) never retry: a failed data test is not transient,
  and retrying it only delays the alert. Everything after a gate is skipped when the gate fails.
* **Incremental by watermark**: ingest reads its own watermarks (``audit.ingest_watermark``), so a
  normal run passes no dates. ``full_refresh`` is the only way to reload everything.
* **Backfill**: with ``simulate_source_day`` off, the feed step processes exactly the run's logical
  date (``--date ds``) and dbt reports as of that date, so each backfilled day is rerunnable.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path

TARGETS: tuple[str, ...] = ("duckdb", "postgres", "snowflake")

# BashOperator's default skip_on_exit_code: the task ends as "skipped", not "failed".
SKIP_EXIT_CODE = 99


@dataclass(frozen=True)
class Step:
    name: str
    module: str
    doc: str
    gate: bool = False  # failing blocks every later step
    retries: int = 2  # retries for transient failures (locks, network); gates use 0
    timeout_minutes: int = 20
    depends_on_past: bool = False  # True where yesterday must finish first (watermarks move forward)


STEPS: tuple[Step, ...] = (
    Step("generate", "generator", "Simulate one more day of Admin Core work and a new landing folder (demo only).",
         retries=1),
    Step("feed", "ingest.feed", "Validate landing files against the manifest and data contracts, quarantine bad "
         "rows, promote good deals and remittances into Admin Core.", depends_on_past=True),
    Step("ingest", "ingest", "CDC by change-tracking version (Admin Core) and updated_at watermark (legacy), plus "
         "raw feed files, into raw_* with audit columns.", depends_on_past=True),
    Step("dbt", "dbt.cli.main", "dbt build: seeds, staging, intermediate, marts, snapshots and every test.",
         gate=True, retries=0, timeout_minutes=30),
    Step("diagnostics", "pipeline.diagnostics", "Diagnostics gate: match rates, key-bridge coverage, fan-out, "
         "claim sign, demo exclusion, freshness, posting periods.", gate=True, retries=0),
    Step("reconcile", "pipeline.reconcile", "Reconciliation gate: row counts and dollar totals, source vs raw vs "
         "marts.", gate=True, retries=0),
    Step("actuarial", "actuarial", "Earning curves, loss triangles, IBNR, early-claim emergence."),
    Step("semantic", "semantic", "Generate the Power BI-ready semantic model spec from metrics.yml."),
    Step("publish", "publish", "Aggregate data products and per-dealer packets."),
    Step("reports", "reports", "Report site (HTML + Excel)."),
)

STEP_NAMES: tuple[str, ...] = tuple(s.name for s in STEPS)
BY_NAME: dict[str, Step] = {s.name: s for s in STEPS}


@dataclass(frozen=True)
class RunParams:
    """What one DAG run asks for (the DAG's params plus the logical date)."""

    target: str = "duckdb"
    ds: str | None = None  # logical date, YYYY-MM-DD
    simulate_source_day: bool = True  # demo mode: generate a new day first
    full_refresh: bool = False

    def __post_init__(self) -> None:
        if self.target not in TARGETS:
            raise ValueError(f"unknown dbt target {self.target!r}; expected one of {TARGETS}")


def argv(step: str, p: RunParams, python: str = "python") -> list[str] | None:
    """The command for one step, or None when the step has nothing to do in this run."""
    s = BY_NAME[step]
    cmd = [python, "-m", s.module]
    if step == "generate":
        return [*cmd, "--append-day", "1"] if p.simulate_source_day else None
    if step == "feed":
        # Demo mode: process every pending folder (the generator's new day). Backfill: exactly ds.
        return cmd if p.simulate_source_day or not p.ds else [*cmd, "--date", p.ds]
    if step == "ingest":
        return [*cmd, "--full-refresh"] if p.full_refresh else cmd
    if step == "dbt":
        cmd += ["build", "--project-dir", "dbt", "--profiles-dir", "dbt", "--target", p.target]
        if p.full_refresh:
            cmd.append("--full-refresh")
        if not p.simulate_source_day and p.ds:
            cmd += ["--vars", json.dumps({"as_of_date": p.ds})]
        return cmd
    return cmd


def bash_command(step: str, p: RunParams, repo: Path | str, python: str = "python") -> str:
    """Shell line for a BashOperator. A step with nothing to do exits 99, which Airflow marks skipped."""
    cmd = argv(step, p, python)
    if cmd is None:
        return f"echo 'step {step}: nothing to do in this run' && exit {SKIP_EXIT_CODE}"
    return f"cd {shlex.quote(str(repo))} && {shlex.join(cmd)}"


def step_env(repo: Path | str, target: str) -> dict[str, str]:
    """Environment for every step (docs/V3_DESIGN.md 2.1). Same paths pipeline.run uses."""
    root = Path(repo)
    return {
        "ADMIN_CORE_PATH": str(root / "data" / "source" / "admin_core.db"),
        "LEGACY_DB_PATH": str(root / "data" / "source" / "legacy_admin.db"),
        "LANDING_DIR": str(root / "data" / "landing"),
        "WAREHOUSE_PATH": str(root / "data" / "warehouse" / "warehouse.duckdb"),
        "DBT_TARGET": target,
        "PYTHONUNBUFFERED": "1",
        "PYTHONIOENCODING": "utf-8",
    }
