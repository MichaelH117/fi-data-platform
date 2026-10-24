"""Opt-in Postgres target: publish step, semantic --target postgres, pipeline --postgres.

Unit tests always run. Tests marked ``postgres_live`` need the local server (tools/pg.ps1 start, .env) and
a synced warehouse; they skip when either is missing, so CI (no Postgres) stays green.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from pipeline import run as pr
from semantic.__main__ import main as semantic_main
from semantic.build import TARGET_OUT_DIRS, generate
from warehouse.postgres import sync as pg

ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))


# ---------------------------------------------------------------------- unit
def test_env_file_is_git_ignored_and_example_has_no_secrets():
    out = subprocess.run(["git", "check-ignore", ".env"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, ".env must be git-ignored"
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in example.splitlines():
        if "PASSWORD=" in line:
            assert line.split("=", 1)[1].startswith("<"), line


def test_declared_keys_come_from_dbt_tests():
    keys = pg.declared_keys()
    assert keys["fct_earned_disbursement"] == ("contract_key", "disbursement_type", "posting_period_key",
                                               "earned_month_key")
    assert keys["fct_cancellation"] == ("cancel_key",)


def test_type_map_and_unknown_type_fails():
    t = pg.Table("core", "x", [])
    assert t.pg_type("DOUBLE") == "double precision"
    assert t.pg_type("DECIMAL(18,2)") == "numeric(18,2)"
    assert t.pg_type("TIMESTAMP WITH TIME ZONE") == "timestamptz"
    with pytest.raises(pg.SyncError):
        t.pg_type("MAP(VARCHAR, INTEGER)")


def test_reconcile_tolerance():
    assert pg._close(10, 10, exact=True) and not pg._close(10, 11, exact=True)
    assert pg._close(1_000_000.004, 1_000_000.0, exact=False)
    assert not pg._close(100.0, 100.5, exact=False)
    assert pg._close(None, None, exact=False) and not pg._close(None, 0.0, exact=False)


def test_settings_read_env_without_leaking(monkeypatch):
    s = pg.Settings.from_env({"PG_PORT": "5544", "PG_DATABASE": "db1", "PG_BI_READER_PASSWORD": "x"})
    assert (s.port, s.database, s.reader, s.reader_password) == (5544, "db1", "bi_reader", "x")


def test_semantic_postgres_target_sources(tmp_path):
    generate(tmp_path / "model", target="postgres")
    tm = list((tmp_path / "model").rglob("*.tmdl"))
    text = "\n".join(p.read_text(encoding="utf-8") for p in tm)
    assert "PostgreSQL.Database(PostgresServer, PostgresDatabase)" in text
    assert "Snowflake.Databases" not in text
    assert 'expression PostgresServer = "localhost:5432"' in text
    assert 'expression PostgresDatabase = "vsc_analytics"' in text
    assert "mode: import" in text and "directQuery" not in text
    assert "sourceColumn: DEALER_KEY" not in text and "sourceColumn: dealer_key" in text
    assert "[posting_period_key] >= Date.Year(RangeStart)" in text


def test_semantic_default_target_is_snowflake(tmp_path):
    generate(tmp_path / "model")
    text = (tmp_path / "model" / "Sales.SemanticModel" / "definition" / "expressions.tmdl").read_text("utf-8")
    assert "SnowflakeServer" in text and "Postgres" not in text


def test_both_committed_models_are_current():
    assert TARGET_OUT_DIRS["postgres"].exists(), "run python -m semantic --target postgres"
    assert semantic_main(["--check"]) == 0
    assert semantic_main(["--check", "--target", "postgres"]) == 0


def test_pipeline_postgres_flag_runs_sync_after_steps(tmp_path):
    args = pr.parse_args(["--only", "feed", "--postgres"])
    calls: list[list[str]] = []

    def runner(argv, env):
        calls.append(list(argv))
        return 0

    summary = pr.run_pipeline(args, runner=runner, runs_dir=tmp_path, warehouse=tmp_path / "w.duckdb")
    assert calls[-1][1:4] == ["-m", "warehouse.postgres", "sync"]
    assert summary.steps[-1].name == "postgres" and summary.steps[-1].status == "ok"
    assert "postgres" not in pr.STEPS  # opt-in only: never part of the DAG / CI


def test_pipeline_postgres_failure_fails_the_run(tmp_path):
    args = pr.parse_args(["--only", "feed", "--postgres"])
    summary = pr.run_pipeline(args, runner=lambda a, e: 2 if "warehouse.postgres" in a else 0,
                              runs_dir=tmp_path, warehouse=tmp_path / "w.duckdb")
    assert summary.status == "failed" and "postgres" in summary.error


def test_pipeline_default_has_no_postgres():
    assert pr.parse_args([]).postgres is False


# ---------------------------------------------------------------------- live server
def _live_or_skip() -> pg.Settings:
    s = pg.Settings.from_env()
    reason = pg.available(s)
    if reason:
        pytest.skip(f"Postgres not available: {reason}")
    if not WAREHOUSE.exists():
        pytest.skip("no warehouse built")
    with s.connect() as con:
        if not con.execute("select to_regclass('core.dim_dealer')").fetchone()[0]:
            pytest.skip("not synced yet (python -m warehouse.postgres sync)")
    return s


def test_live_published_copy_reconciles():
    s = _live_or_skip()
    problems, totals = pg.reconcile_only(WAREHOUSE, s)
    assert not problems, problems[:10]
    assert totals["tables"] > 50 and totals["rows"] > 1_000_000


def test_live_bi_reader_reads_but_cannot_write():
    s = _live_or_skip()
    psycopg = pytest.importorskip("psycopg")
    with s.connect(reader=True, autocommit=True) as con:
        n = con.execute("select count(*) from core.dim_dealer").fetchone()[0]
        assert n > 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            con.execute("delete from core.dim_dealer")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            con.execute("select count(*) from security.user_dealer_access")


def test_live_keys_and_rls_in_place():
    s = _live_or_skip()
    with s.connect() as con:
        pk = con.execute("select count(*) from pg_constraint c join pg_namespace n on n.oid = c.connamespace "
                         "where c.contype = 'p' and n.nspname = 'core'").fetchone()[0]
        rls = con.execute("select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                          "where c.relrowsecurity and n.nspname = 'core'").fetchone()[0]
    assert pk >= 20 and rls >= 10


def test_live_bi_reader_sees_every_row():
    """The import login bypasses the Postgres row policies (the semantic model applies RLS), so it gets the
    unknown-dealer member too, and its counts equal the superuser's."""
    s = _live_or_skip()
    q = "select count(*) from core.dim_dealer"
    with s.connect() as su, s.connect(reader=True) as bi:
        assert bi.execute(q).fetchone()[0] == su.execute(q).fetchone()[0]
