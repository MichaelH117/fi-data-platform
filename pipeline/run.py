"""End-to-end local pipeline (docs/V3_DESIGN.md section 1).

    generate -> feed -> ingest -> dbt -> diagnostics -> reconcile -> actuarial -> semantic -> publish -> reports
                                  \\______________ gates ______________/

Usage::

    python -m pipeline.run                    # full run (generate only if Admin Core is missing)
    python -m pipeline.run --append-day       # simulate a new day of source activity first
    python -m pipeline.run --from dbt         # resume from a step
    python -m pipeline.run --only publish reports
    python -m pipeline.run --env uat          # build into the UAT paths (pipeline/envs.py), then
    python tools/env_diff.py                  # compare UAT with prod before promoting

Each step runs as its own ``python -m`` subprocess (same contract the Airflow DAG and CI use),
is timed and logged, and the run fails fast on the first non-zero exit.

Gate rule: actuarial, semantic, publish and reports never run unless dbt, diagnostics and
reconcile passed - in this run, or (for ``--from`` / ``--only``) in the last run that executed
them, as recorded in ``data/runs/_gates.json``.

A JSON summary is written to ``data/runs/<run_id>.json`` next to a full log
``data/runs/<run_id>.log``, and one row per run is kept in ``audit.pipeline_run``.

So the site can show the run that builds it, a provisional record (status ``running``, the steps so
far, gates passed) is written to both places just before the reports step and replaced when the run
ends. The dbt build's ``run_results.json`` is copied to ``data/runs/<run_id>/dbt_build.json`` right
after the dbt step, because the diagnostics and reconcile gates run ``dbt test`` and overwrite it.

Write-audit-publish: the dbt step never builds into the live warehouse. It copies the live file to
``<warehouse dir>/_staged/<same file name>`` (raw already ingested), builds the marts there, and the diagnostics and
reconcile gates audit that staged file. Only when dbt, diagnostics and reconcile have all passed on
the same staged build is it swapped in with an atomic rename (``publish_build``). When a gate fails,
the live warehouse keeps the last build that passed every gate (core, finance, claims, dealer and
regulatory are untouched), the staged file stays for debugging, and the gate rows of the run are
copied into the live ``audit`` tables so the failure is visible. ``--from diagnostics`` /
``--from reconcile`` re-audit a staged build still waiting to be published. The build that is live is
kept in ``data/runs/_live_build.json`` and in ``audit.pipeline_run.live_build``. On Snowflake the same
pattern is a build into ``<schema>_STAGED`` and ``ALTER SCHEMA <schema> SWAP WITH <schema>_STAGED``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pipeline.envs import ENVS
from pipeline.envs import activate as activate_env

ROOT = Path(__file__).resolve().parents[1]
RUNS_DIR = Path(os.environ.get("PIPELINE_RUNS_DIR", ROOT / "data" / "runs"))  # override for isolated runs
# SOURCE_DB_PATH is the retired V1 name for the legacy database; accepted as an alias for one release.
ADMIN_CORE_DB = Path(os.environ.get("ADMIN_CORE_PATH", ROOT / "data" / "source" / "admin_core.db"))
LEGACY_DB = Path(os.environ.get("LEGACY_DB_PATH", os.environ.get("SOURCE_DB_PATH",
                                                                 ROOT / "data" / "source" / "legacy_admin.db")))
LANDING_DIR = Path(os.environ.get("LANDING_DIR", ROOT / "data" / "landing"))
WAREHOUSE_DB = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))
DBT_DIR = ROOT / "dbt"

STEPS: tuple[str, ...] = (
    "generate", "feed", "ingest", "dbt", "diagnostics", "reconcile", "actuarial", "semantic", "publish", "reports",
)
GATES: tuple[str, ...] = ("dbt", "diagnostics", "reconcile")
GATED: tuple[str, ...] = ("actuarial", "semantic", "publish", "reports")
GATES_FILE = "_gates.json"
DBT_BUILD_FILE = "dbt_build.json"  # data/runs/<run_id>/dbt_build.json: the build's run_results.json
LIVE_BUILD_FILE = "_live_build.json"  # data/runs/_live_build.json: the dbt build the live warehouse holds
# Gate audit rows copied from a held (unpublished) staged build into the live warehouse.
GATE_AUDIT_TABLES: tuple[str, ...] = ("diagnostics_result", "reconciliation", "layer_row_count")

# Optional step modules: skipped (not failed) until the owning team ships them.
OPTIONAL_MODULES: dict[str, Path] = {"semantic": ROOT / "semantic" / "__main__.py"}

log = logging.getLogger("pipeline")

# A runner takes (argv, env) and returns the exit code. Swappable so tests never spawn processes.
Runner = Callable[[Sequence[str], dict[str, str]], int]


class PipelineError(RuntimeError):
    """Raised when a step exits non-zero or a gate blocks a step."""


@dataclass
class StepResult:
    name: str
    status: str  # ok | failed | skipped | blocked
    seconds: float = 0.0
    command: list[str] = field(default_factory=list)
    returncode: int | None = None
    note: str = ""


@dataclass
class RunSummary:
    run_id: str
    started_at: str
    finished_at: str = ""
    status: str = "running"
    seconds: float = 0.0
    args: dict[str, object] = field(default_factory=dict)
    steps: list[StepResult] = field(default_factory=list)
    error: str = ""
    live_build: str = ""  # run_id of the dbt build the live warehouse holds after this run
    wap: str = ""  # write-audit-publish outcome: published | held | "" (no staged build this run)


def skip_reason(name: str, args: argparse.Namespace) -> str | None:
    """Why a step has nothing to do this run, or None when it should run."""
    if name == "generate" and not args.append_day and ADMIN_CORE_DB.exists():
        if args.seed is not None and source_seed(ADMIN_CORE_DB) != args.seed:
            return None  # --seed names a different world than the one on disk: rebuild it
        return "source databases exist (pass --append-day for new activity)"
    if name in OPTIONAL_MODULES and not OPTIONAL_MODULES[name].exists():
        return f"module {name}/ not built yet"
    return None


def source_seed(db: Path) -> int | None:
    """Seed stamped into a source DB by the generator (``_source_epoch``), or None for an older build."""
    from generator import read_source_epoch

    epoch = read_source_epoch(db)
    return int(epoch["seed"]) if epoch and epoch.get("seed") is not None else None


def source_reset_in_run(warehouse: Path, run_id: str) -> list[str]:
    """Sources whose raw data the ingest of this run dropped and reloaded (``audit.source_epoch``).
    After a reset, dbt incrementals and snapshots hold rows from the old world and must be rebuilt."""
    if not run_id or not warehouse.exists():
        return []
    try:
        import duckdb

        with duckdb.connect(str(warehouse), read_only=True) as con:
            rows = con.execute("select source_system from audit.source_epoch where reset_run_id = ?",
                               [run_id]).fetchall()
    except Exception:  # noqa: BLE001 - no audit table yet means nothing was reset
        return []
    return sorted(r[0] for r in rows)


def drop_snapshots(warehouse: Path) -> int:
    """Drop the SCD2 snapshot tables (dbt ignores --full-refresh for snapshots). Returns tables dropped."""
    import duckdb

    with duckdb.connect(str(warehouse)) as con:
        tables = con.execute("select table_name from information_schema.tables "
                             "where table_schema = 'snapshots' and table_type = 'BASE TABLE'").fetchall()
        for (t,) in tables:
            con.execute(f'drop table if exists snapshots."{t}"')
    return len(tables)


def step_command(name: str, args: argparse.Namespace) -> list[str] | None:
    """Return the argv for a step, or None when the step has nothing to do this run."""
    py = sys.executable
    if skip_reason(name, args):
        return None
    if name == "generate":
        if args.append_day:
            return [py, "-m", "generator", "--append-day", str(args.append_day)]
        return [py, "-m", "generator", *(["--seed", str(args.seed)] if args.seed is not None else [])]
    if name == "feed":
        return [py, "-m", "ingest.feed"]
    if name == "ingest":
        # Raw is the only replayable copy of every source version (int_*_versions rebuild SCD2 history
        # from it), so --full-refresh rebuilds dbt only; dropping raw takes the explicit --reload-raw.
        return [py, "-m", "ingest", *(["--full-refresh"] if getattr(args, "reload_raw", False) else [])]
    if name == "dbt":
        # The gate tests (tag:gate) run in the diagnostics and reconcile steps, which audit them.
        cmd = [py, "-m", "dbt.cli.main", "build", "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR),
               "--exclude", "tag:gate"]
        return cmd + (["--full-refresh"] if args.full_refresh else [])
    if name in ("diagnostics", "reconcile"):
        return [py, "-m", f"pipeline.{name}"]
    if name in ("actuarial", "semantic", "publish", "reports"):
        return [py, "-m", name]
    raise ValueError(f"unknown step {name!r}")


def select_steps(only: Sequence[str] | None, start: str | None) -> list[str]:
    """Resolve --only / --from into an ordered list of steps (always in pipeline order)."""
    if only and start:
        raise ValueError("use --only or --from, not both")
    if only:
        return [s for s in STEPS if s in set(only)]
    if start:
        return list(STEPS[STEPS.index(start) :])
    return list(STEPS)


def subprocess_runner(argv: Sequence[str], env: dict[str, str]) -> int:
    """Run a child process, streaming its output into the pipeline log line by line."""
    with subprocess.Popen(
        list(argv),
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    ) as proc:
        assert proc.stdout is not None
        for line in proc.stdout:
            log.info("  | %s", line.rstrip())
        return proc.wait()


def child_env(run_id: str = "") -> dict[str, str]:
    env = dict(os.environ)
    env.pop("SOURCE_DB_PATH", None)  # retired alias: children get the explicit V3 variables
    env.setdefault("ADMIN_CORE_PATH", str(ADMIN_CORE_DB))
    env.setdefault("LEGACY_DB_PATH", str(LEGACY_DB))
    env.setdefault("LANDING_DIR", str(LANDING_DIR))
    env.setdefault("WAREHOUSE_PATH", str(WAREHOUSE_DB))
    env.setdefault("DO_NOT_TRACK", "1")  # dbt: never call home from a local run
    if run_id:
        env["PIPELINE_RUN_ID"] = run_id
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def read_gates(runs_dir: Path) -> dict[str, dict[str, str]]:
    try:
        return json.loads((runs_dir / GATES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_gate(runs_dir: Path, gate: str, status: str, run_id: str) -> None:
    gates = read_gates(runs_dir)
    gates[gate] = {"status": status, "run_id": run_id, "at": datetime.now(UTC).isoformat(timespec="seconds")}
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / GATES_FILE).write_text(json.dumps(gates, indent=2), encoding="utf-8")


def gate_block(passed_now: set[str], runs_dir: Path) -> str | None:
    """Reason a gated step must not run, or None. Gates not run in this run must have passed last time."""
    recorded = read_gates(runs_dir)
    for gate in GATES:
        if gate in passed_now:
            continue
        status = recorded.get(gate, {}).get("status", "never run")
        if status != "ok":
            return f"gate '{gate}' is {status!r}; fix it, then run: python -m pipeline.run --from {gate}"
    return None


def staged_path(live: Path) -> Path:
    """Where the dbt step builds and the gates audit: ``warehouse.duckdb`` -> ``_staged/warehouse.duckdb``.

    The file name must stay the same: DuckDB names the catalog after the file stem, and dbt writes
    that catalog into every view definition, so a staged file with another name would publish views
    that point at a catalog the live file does not have (and a dot in the stem does not parse)."""
    return live.parent / "_staged" / live.name


def _wal(path: Path) -> Path:
    return path.with_name(path.name + ".wal")


def stage_build(live: Path, staged: Path) -> None:
    """Start a staged build from a copy of the live warehouse (raw, audit and the current marts), so
    incrementals and snapshots continue from the live state. No live file: dbt starts the staged one."""
    for p in (staged, _wal(staged)):
        p.unlink(missing_ok=True)
    staged.parent.mkdir(parents=True, exist_ok=True)
    if not live.exists():
        return
    if _wal(live).exists():  # fold a pending WAL into the file before copying it
        import duckdb

        with duckdb.connect(str(live)) as con:
            con.execute("checkpoint")
    shutil.copyfile(live, staged)


def gates_passed_on_build(runs_dir: Path) -> bool:
    """True when diagnostics and reconcile passed in or after the run that built the staged file."""
    gates = read_gates(runs_dir)
    build = gates.get("dbt", {})
    if build.get("status") != "ok":
        return False
    return all(gates.get(g, {}).get("status") == "ok" and gates[g].get("run_id", "") >= build.get("run_id", "")
               for g in GATES)


def read_live_build(runs_dir: Path) -> str:
    try:
        return str(json.loads((runs_dir / LIVE_BUILD_FILE).read_text(encoding="utf-8")).get("build_run_id", ""))
    except (OSError, ValueError):
        return ""


def publish_build(staged: Path, live: Path, runs_dir: Path) -> str:
    """Swap the audited staged build in as the live warehouse (atomic rename). Returns its build run_id."""
    build = read_gates(runs_dir).get("dbt", {}).get("run_id", "")
    try:
        if _wal(staged).exists():
            import duckdb

            with duckdb.connect(str(staged)) as con:
                con.execute("checkpoint")
        _wal(live).unlink(missing_ok=True)  # a WAL belongs to the file it is replacing
        os.replace(staged, live)
    except OSError as exc:
        raise PipelineError(f"the staged build passed every gate but could not replace {live} ({exc}); "
                            "close any program that has the warehouse open, then run: "
                            "python -m pipeline.run --from reconcile") from exc
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / LIVE_BUILD_FILE).write_text(json.dumps(
        {"build_run_id": build, "published_at": datetime.now(UTC).isoformat(timespec="seconds")}, indent=2),
        encoding="utf-8")
    log.info("[publish-build] staged build %s passed dbt, diagnostics and reconcile; it is now live", build)
    return build


def copy_gate_audit(staged: Path, live: Path, run_id: str) -> None:
    """Copy this run's gate rows from a held staged build into the live audit tables (best effort)."""
    if not (staged.exists() and live.exists()):
        return
    try:
        import duckdb

        with duckdb.connect(str(live)) as con:
            con.execute(f"attach '{staged.as_posix()}' as staged_build (read_only)")
            con.execute("create schema if not exists audit")
            for t in GATE_AUDIT_TABLES:
                exists = con.execute("select count(*) from duckdb_tables() where database_name = 'staged_build' "
                                     "and schema_name = 'audit' and table_name = ?", [t]).fetchone()[0]
                if not exists:
                    continue
                con.execute(f"create table if not exists audit.{t} as select * from staged_build.audit.{t} limit 0")
                con.execute(f"delete from audit.{t} where run_id = ?", [run_id])
                con.execute(f"insert into audit.{t} by name select * from staged_build.audit.{t} where run_id = ?",
                            [run_id])
            con.execute("detach staged_build")
    except Exception as exc:  # noqa: BLE001 - the JSON summary still records the failed gate
        log.warning("could not copy gate audit rows from the staged build: %s", exc)


def run_pipeline(args: argparse.Namespace, runner: Runner = subprocess_runner, runs_dir: Path = RUNS_DIR,
                 warehouse: Path | None = None) -> RunSummary:
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    summary = RunSummary(run_id=run_id, started_at=datetime.now(UTC).isoformat(timespec="seconds"))
    summary.args = {k: v for k, v in vars(args).items() if not callable(v)}
    steps = select_steps(args.only, args.start)
    env = child_env(run_id)
    live = warehouse if warehouse is not None else Path(env["WAREHOUSE_PATH"])
    staged = staged_path(live)
    audit_env = {**env, "WAREHOUSE_PATH": str(staged)}  # dbt builds and the gates audit the staged copy
    summary.live_build = read_live_build(runs_dir)
    passed_gates: set[str] = set()
    t_run = time.perf_counter()
    log.info("run %s: steps %s", run_id, " -> ".join(steps))
    try:
        for name in steps:
            if name in GATED:
                reason = gate_block(passed_gates, runs_dir)
                if reason:
                    summary.steps.append(StepResult(name, "blocked", note=reason))
                    raise PipelineError(f"step '{name}' blocked: {reason}")
            cmd = step_command(name, args)
            if cmd is None:
                note = skip_reason(name, args) or "nothing to do"
                log.info("[%s] skipped (%s)", name, note)
                summary.steps.append(StepResult(name, "skipped", note=note))
                continue
            if name == "reports":
                # Provisional record so the site sees this run (status 'running', gates passed so far).
                summary.seconds = round(time.perf_counter() - t_run, 2)
                _write_summary(summary, runs_dir)
                _audit_run(summary, warehouse if warehouse is not None else Path(env["WAREHOUSE_PATH"]))
            if name == "dbt":
                stage_build(live, staged)
                log.info("[dbt] building into the staged copy %s; %s stays live until every gate passes",
                         f"{staged.parent.name}/{staged.name}", live.name)
            if name == "dbt" and "--full-refresh" not in cmd:
                reset = source_reset_in_run(live, run_id)
                if reset:
                    log.info("[dbt] source reset this run (%s): full refresh, %d snapshot tables dropped",
                             ", ".join(reset), drop_snapshots(staged) if staged.exists() else 0)
                    cmd = [*cmd, "--full-refresh"]
            # dbt builds into the staged copy; the gates audit it while one waits to be published
            staged_step = name in GATES and (name == "dbt" or staged.exists())
            step_env = audit_env if staged_step else env
            if staged_step:
                summary.wap = "held"
            log.info("[%s] start: %s", name, " ".join(cmd[1:]))
            t0 = time.perf_counter()
            code = runner(cmd, step_env)
            secs = round(time.perf_counter() - t0, 2)
            ok = code == 0
            if name == "dbt":
                _save_dbt_build(runs_dir, run_id, since=time.time() - secs - 1)
                if ok:
                    _source_freshness(cmd, step_env, runner)
            summary.steps.append(StepResult(name, "ok" if ok else "failed", secs, list(cmd), code))
            if name in GATES:
                write_gate(runs_dir, name, "ok" if ok else "failed", run_id)
                if ok:
                    passed_gates.add(name)
                if ok and name != "dbt" and staged.exists() and gates_passed_on_build(runs_dir):
                    summary.live_build = publish_build(staged, live, runs_dir)
                    summary.wap = "published"
            if not ok:
                raise PipelineError(
                    f"step '{name}' failed with exit code {code} after {secs}s; "
                    f"fix it, then resume with: python -m pipeline.run --from {name}"
                )
            log.info("[%s] ok in %.2fs", name, secs)
        if summary.wap == "held":
            log.warning("staged build %s is not live yet: diagnostics and reconcile must pass on it "
                        "(python -m pipeline.run --from diagnostics)", staged.name)
        if getattr(args, "postgres", False):
            _postgres_step(summary, runner, env, live)
        summary.status = "ok"
    except PipelineError as exc:
        summary.status, summary.error = "failed", str(exc)
        log.error(str(exc))
        if summary.wap == "held":
            copy_gate_audit(staged, live, run_id)
            log.error("the staged build was NOT published: %s still holds build %s, the last one that passed "
                      "every gate", live.name, summary.live_build or "(from before write-audit-publish)")
    finally:
        summary.seconds = round(time.perf_counter() - t_run, 2)
        summary.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
        _write_summary(summary, runs_dir)
        _audit_run(summary, warehouse if warehouse is not None else Path(env["WAREHOUSE_PATH"]))
    return summary


def _postgres_step(summary: RunSummary, runner: Runner, env: dict[str, str], live: Path) -> None:
    """Opt-in (--postgres): copy the live, gate-passed warehouse to the local Postgres server and reconcile.

    Not in STEPS (the Airflow DAG and CI never run it). Refuses a held build: only a published warehouse,
    one that passed every gate, is copied."""
    if summary.wap == "held":
        summary.steps.append(StepResult("postgres", "blocked", note="staged build not published"))
        raise PipelineError("step 'postgres' blocked: the staged build did not pass every gate")
    cmd = [sys.executable, "-m", "warehouse.postgres", "sync", "--warehouse", str(live)]
    log.info("[postgres] start: %s", " ".join(cmd[1:]))
    t0 = time.perf_counter()
    code = runner(cmd, env)
    secs = round(time.perf_counter() - t0, 2)
    summary.steps.append(StepResult("postgres", "ok" if code == 0 else "failed", secs, cmd, code))
    if code != 0:
        raise PipelineError(f"step 'postgres' failed with exit code {code} after {secs}s (2 = server not "
                            "available: tools/pg.ps1 start); retry with: python -m warehouse.postgres sync")
    log.info("[postgres] ok in %.2fs", secs)


def freshness_command(build_cmd: Sequence[str]) -> list[str]:
    """``dbt source freshness`` with the same interpreter, project and profile as the dbt build step."""
    i = list(build_cmd).index("build")
    return [*build_cmd[:i], "source", "freshness", "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR)]


def _source_freshness(build_cmd: Sequence[str], env: dict[str, str], runner) -> int:
    """Run ``dbt source freshness`` after the build so target/sources.json (the quality page's
    freshness tile) is current. Informational: a stale source is reported, never fatal here (the
    raw-age diagnostic gate is what blocks publishing)."""
    code = runner(freshness_command(build_cmd), env)
    if code:
        log.warning("[dbt] source freshness exit %s: a source is stale or freshness could not run", code)
    return code


def _write_summary(summary: RunSummary, runs_dir: Path) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{summary.run_id}.json"
    path.write_text(json.dumps(asdict(summary), indent=2), encoding="utf-8")
    log.info("run %s %s in %.1fs; summary -> %s", summary.run_id, summary.status.upper(), summary.seconds, path)
    return path


def dbt_target_dir() -> Path:
    """Where dbt writes run_results.json (DBT_TARGET_PATH is relative to the dbt project)."""
    t = Path(os.environ.get("DBT_TARGET_PATH", "target"))
    return t if t.is_absolute() else DBT_DIR / t


def _save_dbt_build(runs_dir: Path, run_id: str, since: float = 0.0) -> Path | None:
    """Copy the dbt build's run_results.json to data/runs/<run_id>/dbt_build.json before the gate
    steps (dbt test) overwrite it. Only a file written during this step is copied."""
    src = dbt_target_dir() / "run_results.json"
    try:
        if not src.exists() or src.stat().st_mtime < since:
            log.warning("dbt wrote no run_results.json this step; build results not saved")
            return None
        dest = runs_dir / run_id / DBT_BUILD_FILE
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return dest
    except OSError as exc:
        log.warning("could not save dbt build results: %s", exc)
        return None


PIPELINE_RUN_DDL = """
create schema if not exists audit;
create table if not exists audit.pipeline_run (
    run_id varchar, started_at timestamp, finished_at timestamp, status varchar, seconds double,
    steps varchar, error varchar, args varchar
);
alter table audit.pipeline_run add column if not exists live_build varchar;
"""


def _audit_run(summary: RunSummary, warehouse: Path) -> None:
    """Upsert the run into audit.pipeline_run (the provisional 'running' row of the run is
    replaced by the final one). Never fails the run (the JSON summary is the fallback)."""
    if not warehouse.exists():
        return
    try:
        import duckdb

        with duckdb.connect(str(warehouse)) as con:
            con.execute(PIPELINE_RUN_DDL)
            con.execute("delete from audit.pipeline_run where run_id = ? and status = 'running'", [summary.run_id])
            con.execute(
                "insert into audit.pipeline_run (run_id, started_at, finished_at, status, seconds, steps, error, "
                "args, live_build) values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    summary.run_id,
                    datetime.fromisoformat(summary.started_at).replace(tzinfo=None),
                    # provisional rows have no finish yet: use 'now' so ordering by finished_at holds
                    datetime.fromisoformat(summary.finished_at or datetime.now(UTC).isoformat()).replace(tzinfo=None),
                    summary.status,
                    summary.seconds,
                    json.dumps([asdict(s) for s in summary.steps]),
                    summary.error,
                    json.dumps(summary.args, default=str),
                    summary.live_build,
                ],
            )
    except Exception as exc:  # noqa: BLE001 - audit is best effort; a locked file must not fail the run
        log.warning("could not append audit.pipeline_run: %s", exc)


def _setup_logging(runs_dir: Path) -> None:
    runs_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    stream = logging.StreamHandler(sys.stdout)
    file = logging.FileHandler(runs_dir / f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}.log", encoding="utf-8")
    for h in (stream, file):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python -m pipeline.run", description="Run the local data platform end to end.")
    pick = ap.add_mutually_exclusive_group()
    pick.add_argument("--from", dest="start", choices=STEPS, help="start at this step and run the rest")
    pick.add_argument("--only", nargs="+", choices=STEPS, help="run only these steps (in pipeline order)")
    ap.add_argument(
        "--append-day",
        type=int,
        nargs="?",
        const=1,
        default=0,
        metavar="DAYS",
        help="append DAYS (default 1) of new source activity before ingesting",
    )
    ap.add_argument("--seed", type=int, help="generator seed for fresh source dbs (default: generator's)")
    ap.add_argument("--full-refresh", action="store_true",
                    help="rebuild dbt incrementals; raw (every source version) is kept")
    ap.add_argument("--reload-raw", action="store_true",
                    help="also drop the raw schemas and reload every source in full (destroys the raw version "
                    "history; implies --full-refresh)")
    ap.add_argument("--env", choices=ENVS, help="dev | uat | prod: separate warehouse, dbt target, site, "
                    "publish and runs folders (pipeline/envs.py); default: the paths already in the environment")
    ap.add_argument("--postgres", action="store_true",
                    help="after the run, copy the warehouse to the local Postgres server and reconcile it "
                    "(python -m warehouse.postgres sync; needs .env and tools/pg.ps1 start)")
    ns = ap.parse_args(argv)
    ns.full_refresh = ns.full_refresh or ns.reload_raw
    return ns


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    runs_dir = RUNS_DIR
    if args.env:  # separate paths per environment; the generate skip check follows its sources
        global ADMIN_CORE_DB
        selected = activate_env(args.env)
        runs_dir, ADMIN_CORE_DB = selected.runs, selected.admin_core
    _setup_logging(runs_dir)
    return 0 if run_pipeline(args, runs_dir=runs_dir).status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
