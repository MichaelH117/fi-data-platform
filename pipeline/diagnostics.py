"""Diagnostics gate: ``python -m pipeline.diagnostics [--warehouse PATH]``.

Runs the dbt tests tagged ``diagnostics`` (``dbt/tests/diagnostics/``, docs/V3_DESIGN.md section 5),
measures each check's headline number straight from the warehouse, and appends one row per check to
``audit.diagnostics_result``. Exits 1 when any check fails or errors, so ``pipeline.run`` never
reaches actuarial / semantic / publish / reports on bad data.

The checks:
  diag_product_dim_match_rate       product-dim match rate by entered year >= 99.5% (stale-dim trap)
  diag_contract_key_bridge_coverage contract references resolve through the key bridge >= 99%
  diag_claim_contract_coverage      claims that resolve to a contract >= 98% (reported every run)
  diag_fan_out_guard                rows == distinct contract_key on every contract-grain model
  diag_claim_sign                   paid >= 0 after conforming; raw legacy still stored negative
  diag_no_demo_rows                 no demo-dealer or test-range row in any mart
  diag_freshness                    raw loaded within 48 h; bad landing files all quarantined
  diag_posting_period_integrity     every financial fact row has a valid posting_period_key

It also records the row count of every raw source, staging, intermediate, snapshot and mart relation
in the dbt manifest to ``audit.layer_row_count`` (informational, never fails the gate), so the data
quality page shows the layers without any report querying raw or staging tables.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DBT_DIR = ROOT / "dbt"
WAREHOUSE_PATH = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))

log = logging.getLogger("pipeline.diagnostics")

# A runner takes (argv, env) and returns the exit code. Swappable so tests never spawn dbt.
Runner = Callable[[Sequence[str], dict[str, str]], int]

# Headline number per check, measured after the tests run (read-only; NULL when not measurable).
MEASURES: dict[str, tuple[str, float | None]] = {
    "diag_product_dim_match_rate": (
        "select min(m) from (select count(*) filter (where product_key <> '-1') / count(*) as m "
        "from core.dim_contract where contract_key <> '-1' group by entered_year)",
        0.995,
    ),
    "diag_contract_key_bridge_coverage": (
        "select avg(has_contract_key::int) from intermediate.int_claim_number_bridge "
        "where referenced_contract_key is not null",
        0.99,
    ),
    "diag_claim_contract_coverage": (
        "select avg(has_contract_key::int) from intermediate.int_claim_number_bridge",
        0.98,
    ),
    "diag_fan_out_guard": (
        "select count(*) - count(distinct contract_key) from core.dim_contract",
        0.0,
    ),
    "diag_claim_sign": ("select coalesce(min(paid_amount), 0) from core.fct_claim_payment_line", 0.0),
    "diag_no_demo_rows": (
        "select count(*) from core.dim_contract where contract_key in "
        "(select contract_key from intermediate.int_reportable_scope where not is_reportable)",
        0.0,
    ),
    "diag_freshness": (
        "select datediff('hour', max(_loaded_at), current_localtimestamp()) from raw_admin_core.contract",
        48.0,
    ),
    "diag_posting_period_integrity": (
        "select count(*) from core.fct_transaction where posting_period_key not in "
        "(select posting_period_key from core.dim_posting_period)",
        0.0,
    ),
}

DIAGNOSTICS_DDL = """
create schema if not exists audit;
create table if not exists audit.diagnostics_result (
    run_id varchar, check_name varchar, status varchar, failures bigint, measured_value double,
    threshold double, message varchar, checked_at timestamp
);
"""


LAYER_COUNT_DDL = """
create schema if not exists audit;
create table if not exists audit.layer_row_count (
    run_id varchar, layer varchar, schema_name varchar, relation_name varchar, resource_type varchar,
    materialized varchar, row_count bigint, counted_at timestamp
);
"""
MART_SCHEMAS = ("core", "finance", "claims", "dealer", "regulatory")
LAYER_ORDER = ("raw", "staging", "intermediate", "snapshots", "marts")


def layer_of(schema: str, resource_type: str) -> str | None:
    """raw | staging | intermediate | snapshots | marts, or None for relations the page skips
    (seeds, the deprecated V1 compatibility views, audit tables)."""
    if resource_type == "source":
        return "raw" if schema.startswith("raw_") else None
    if resource_type == "snapshot":
        return "snapshots"
    if schema in ("staging", "intermediate"):
        return schema
    return "marts" if schema in MART_SCHEMAS else None


def manifest_relations(manifest: dict) -> list[tuple[str, str, str, str, str]]:
    """(layer, schema, relation, resource_type, materialized) for every countable manifest node."""
    out = []
    for node in [*manifest.get("nodes", {}).values(), *manifest.get("sources", {}).values()]:
        rtype = node.get("resource_type")
        if rtype not in ("model", "snapshot", "source"):
            continue
        cfg = node.get("config", {})
        if rtype == "model" and (cfg.get("materialized") == "ephemeral" or "v1_compat" in node.get("tags", [])):
            continue
        name = node.get("identifier") if rtype == "source" else node.get("alias") or node.get("name")
        layer = layer_of(node.get("schema", ""), rtype)
        if layer and name:
            out.append((layer, node["schema"], name, rtype, cfg.get("materialized") or "table"))
    return sorted(set(out), key=lambda r: (LAYER_ORDER.index(r[0]), r[1], r[2]))


def record_layer_counts(con: duckdb.DuckDBPyConnection, rid: str, manifest_path: Path | None = None) -> int:
    """Count rows per manifest relation into audit.layer_row_count. Returns relations counted;
    a missing manifest or relation is skipped, never an error."""
    path = manifest_path or target_dir() / "manifest.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("no dbt manifest at %s; layer row counts not recorded", path)
        return 0
    con.execute(LAYER_COUNT_DDL)
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = []
    for layer, schema, name, rtype, mat in manifest_relations(manifest):
        try:
            n = con.execute(f'select count(*) from "{schema}"."{name}"').fetchone()[0]
        except duckdb.Error:
            continue
        rows.append((rid, layer, schema, name, rtype, mat, n, now))
    con.execute("delete from audit.layer_row_count where run_id = ?", [rid])
    if rows:
        con.executemany("insert into audit.layer_row_count values (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


class GateError(RuntimeError):
    """A gate could not run at all (dbt crashed, no results)."""


@dataclass
class CheckResult:
    check_name: str
    status: str  # pass | fail | error
    failures: int
    message: str = ""
    measured_value: float | None = None
    threshold: float | None = None

    @property
    def ok(self) -> bool:
        return self.status == "pass"


def run_id() -> str:
    """The pipeline run id when called from pipeline.run, else a fresh UTC stamp."""
    return os.environ.get("PIPELINE_RUN_ID") or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def target_dir() -> Path:
    """Where dbt writes run_results.json (DBT_TARGET_PATH is relative to the dbt project)."""
    t = Path(os.environ.get("DBT_TARGET_PATH", "target"))
    return t if t.is_absolute() else DBT_DIR / t


def subprocess_runner(argv: Sequence[str], env: dict[str, str]) -> int:
    return subprocess.run(list(argv), cwd=ROOT, env=env, check=False).returncode


def dbt_test_command(selector: str, extra: Sequence[str] = ()) -> list[str]:
    return [
        sys.executable, "-m", "dbt.cli.main", "test", "--project-dir", str(DBT_DIR), "--profiles-dir",
        str(DBT_DIR), "--select", selector, *extra,
    ]


def parse_run_results(path: Path, prefix: str) -> list[CheckResult]:
    """One CheckResult per singular test whose name starts with ``prefix``."""
    if not path.exists():
        raise GateError(f"dbt wrote no run results at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for r in data.get("results", []):
        name = r["unique_id"].split(".")[-1]
        if not name.startswith(prefix):
            continue
        status = {"pass": "pass", "fail": "fail", "warn": "fail"}.get(r["status"], "error")
        out.append(CheckResult(name, status, int(r.get("failures") or 0), (r.get("message") or "")[:500]))
    return sorted(out, key=lambda c: c.check_name)


def run_dbt_gate(selector: str, prefix: str, runner: Runner = subprocess_runner,
                 extra: Sequence[str] = ()) -> list[CheckResult]:
    """Run ``dbt test --select <selector>`` and return the per-test results (never raises on a fail)."""
    env = dict(os.environ)
    env.setdefault("WAREHOUSE_PATH", str(WAREHOUSE_PATH))
    env.setdefault("DO_NOT_TRACK", "1")
    results_file = target_dir() / "run_results.json"
    results_file.unlink(missing_ok=True)
    code = runner(dbt_test_command(selector, extra), env)
    results = parse_run_results(results_file, prefix)
    if not results:
        raise GateError(f"dbt test --select {selector} ran no {prefix}* tests (exit code {code})")
    return results


def measure(con: duckdb.DuckDBPyConnection, results: list[CheckResult]) -> None:
    for r in results:
        sql, threshold = MEASURES.get(r.check_name, (None, None))
        r.threshold = threshold
        if sql is None:
            continue
        try:
            value = con.execute(sql).fetchone()[0]
            r.measured_value = None if value is None else float(value)
        except duckdb.Error as exc:  # a missing table is already a failed test; keep the audit row
            log.warning("could not measure %s: %s", r.check_name, exc)


def write_results(con: duckdb.DuckDBPyConnection, rid: str, results: list[CheckResult]) -> None:
    con.execute(DIAGNOSTICS_DDL)
    now = datetime.now(UTC).replace(tzinfo=None)
    con.executemany(
        "insert into audit.diagnostics_result values (?, ?, ?, ?, ?, ?, ?, ?)",
        [(rid, r.check_name, r.status, r.failures, r.measured_value, r.threshold, r.message, now) for r in results],
    )


def run(warehouse: Path = WAREHOUSE_PATH, runner: Runner = subprocess_runner) -> list[CheckResult]:
    results = run_dbt_gate("tag:diagnostics", "diag_", runner)
    with duckdb.connect(str(warehouse)) as con:
        measure(con, results)
        write_results(con, run_id(), results)
        log.info("layer row counts recorded for %d relations", record_layer_counts(con, run_id()))
    return results


def report(results: list[CheckResult], gate: str) -> int:
    bad = [r for r in results if not r.ok]
    for r in results:
        value = "" if r.measured_value is None else f" value={r.measured_value:.6g}"
        log.info("%-36s %-5s failures=%d%s", r.check_name, r.status.upper(), r.failures, value)
    if bad:
        log.error("%s gate FAILED: %s. Publishing is blocked; see docs/operating-model runbook.",
                  gate, ", ".join(r.check_name for r in bad))
        return 1
    log.info("%s gate passed (%d checks)", gate, len(results))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline.diagnostics", description=__doc__.splitlines()[0])
    ap.add_argument("--warehouse", type=Path, default=WAREHOUSE_PATH)
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    os.environ["WAREHOUSE_PATH"] = str(args.warehouse)
    try:
        results = run(args.warehouse)
    except GateError as exc:
        log.error("diagnostics gate could not run: %s", exc)
        return 1
    return report(results, "diagnostics")


if __name__ == "__main__":
    raise SystemExit(main())
