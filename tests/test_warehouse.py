"""Warehouse earning rules: earned facts are never restated after close (docs/V3_DESIGN.md 2.5).

* Unit tests (no warehouse): the earning spine reads the status timeline, books catch-up earning by
  the activation date and reverses VOIDs instead of deleting rows.
* ``warehouse`` tests: the invariants hold on a built warehouse (``WAREHOUSE_PATH``).
* ``slow`` regression: build, ``--append-day``, feed, ingest, rebuild on isolated copies, then the
  earned sum of every closed posting period is unchanged in fct_earned_disbursement and
  fct_contract_monthly.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DBT = ROOT / "dbt"
MONTHS = DBT / "models" / "intermediate" / "int_contract_months.sql"
EARNED = DBT / "models" / "intermediate" / "int_disbursements_earned.sql"
FCT_MONTHLY = DBT / "models" / "marts" / "core" / "fct_contract_monthly.sql"
FCT_EARNED = DBT / "models" / "marts" / "core" / "fct_earned_disbursement.sql"
SINGULAR = DBT / "tests" / "core" / "core_earned_never_restates_closed_periods.sql"


def _sql(path: Path) -> str:
    """Model text without SQL line comments."""
    return re.sub(r"--[^\n]*", "", path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- unit (no warehouse)
def test_spine_does_not_filter_on_current_status() -> None:
    sql = _sql(MONTHS)
    assert not re.search(r"c\.status\s+not\s+in", sql), "earning must not depend on the CURRENT status"
    assert "int_status_history_backfill" in sql and "int_contract_versions" in sql


def test_spine_posts_by_activation_and_reverses_voids() -> None:
    sql = _sql(MONTHS)
    assert "posting_period_join('e.activated_at'" in sql, "catch-up must use the activation date as recorded date"
    assert "posting_period_join('r.voided_at'" in sql, "a VOID reversal posts by the void date"
    assert "'VOID_REVERSAL'" in sql and "earned_month_key" in sql
    assert not re.search(r"\bdelete\s+from\b", sql, re.I)


@pytest.mark.parametrize("path", [EARNED, FCT_EARNED, FCT_MONTHLY])
def test_earned_month_key_carried_to_facts(path: Path) -> None:
    assert "earned_month_key" in _sql(path)


def test_singular_restatement_test_exists() -> None:
    sql = SINGULAR.read_text(encoding="utf-8")
    for needle in ("int_contract_months", "fct_earned_disbursement", "fct_contract_monthly", "int_posting_days"):
        assert needle in sql


# --------------------------------------------------------------------------- built warehouse
def _warehouse() -> Path:
    p = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))
    if not p.exists():
        pytest.skip("no built warehouse")
    return p


def _connect(path: Path):
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(str(path), read_only=True)
    cols = {r[0] for r in con.execute(
        "select column_name from information_schema.columns "
        "where table_schema = 'intermediate' and table_name = 'int_contract_months'").fetchall()}
    if "activated_at" not in cols:
        con.close()
        pytest.skip("warehouse built before the status-timeline earning spine")
    return con


@pytest.mark.warehouse
def test_no_earn_row_posts_before_activation_open_period() -> None:
    con = _connect(_warehouse())
    try:
        bad = con.execute("""
            select count(*)
            from intermediate.int_contract_months as m
            left join intermediate.int_posting_days as d on d.date_day = cast(m.activated_at as date)
            where m.row_type = 'EARN'
              and (m.posting_period_key < m.earned_month_key
                   or m.posting_period_key < coalesce(d.first_open_period_key,
                                                      cast(strftime(m.activated_at, '%Y%m') as integer)))
        """).fetchone()[0]
        pending = con.execute("""
            select count(*)
            from intermediate.int_contract_months as m
            inner join intermediate.int_contracts_conformed as c using (contract_key)
            where c.status = 'PENDING'
        """).fetchone()[0]
    finally:
        con.close()
    assert bad == 0
    assert pending == 0, "a contract still PENDING must not earn"


@pytest.mark.warehouse
def test_facts_tie_to_spine() -> None:
    con = _connect(_warehouse())
    try:
        spine, monthly = con.execute("""
            select (select sum(m.earned_pct) from intermediate.int_contract_months as m
                    where m.contract_key in (select contract_key from core.dim_contract)),
                   (select sum(earned_pct) from core.fct_contract_monthly)
        """).fetchone()
    finally:
        con.close()
    assert monthly == pytest.approx(spine, rel=1e-9, abs=1e-6)


# --------------------------------------------------------------------------- slow regression
CLOSED_SUMS = """
    select 'earned' as fact, e.posting_period_key, round(sum(e.earned_amount), 4) as amount
    from core.fct_earned_disbursement as e
    inner join core.dim_posting_period as p using (posting_period_key)
    where p.is_closed
    group by 1, 2
    union all
    select 'monthly', m.posting_period_key, round(sum(m.earned_premium), 4)
    from core.fct_contract_monthly as m
    inner join core.dim_posting_period as p using (posting_period_key)
    where p.is_closed
    group by 1, 2
"""


def _closed_sums(path: Path) -> dict[tuple[str, int], float]:
    import duckdb

    with duckdb.connect(str(path), read_only=True) as con:
        return {(f, k): v for f, k, v in con.execute(CLOSED_SUMS).fetchall()}


def _run(argv: list[str], env: dict[str, str]) -> None:
    proc = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    assert proc.returncode == 0, f"{' '.join(argv)} failed:\n{proc.stdout[-4000:]}\n{proc.stderr[-4000:]}"


@pytest.mark.slow
@pytest.mark.warehouse
def test_append_day_does_not_restate_closed_periods(tmp_path: Path) -> None:
    """Build -> append-day -> feed -> ingest -> rebuild on copies: closed periods do not move."""
    data = ROOT / "data"
    src = {"admin_core": data / "source" / "admin_core.db", "legacy": data / "source" / "legacy_admin.db"}
    wh = _warehouse()
    if not all(p.exists() for p in src.values()):
        pytest.skip("no source databases")
    pytest.importorskip("duckdb")
    (tmp_path / "source").mkdir()
    for p in src.values():
        shutil.copy2(p, tmp_path / "source" / p.name)
    for d in ("landing", "intake"):
        if (data / d).exists():
            shutil.copytree(data / d, tmp_path / d)
    shutil.copy2(wh, tmp_path / "warehouse.duckdb")
    env = {**os.environ,
           "ADMIN_CORE_PATH": str(tmp_path / "source" / "admin_core.db"),
           "LEGACY_DB_PATH": str(tmp_path / "source" / "legacy_admin.db"),
           "LANDING_DIR": str(tmp_path / "landing"), "INTAKE_DIR": str(tmp_path / "intake"),
           "WAREHOUSE_PATH": str(tmp_path / "warehouse.duckdb"),
           "DBT_TARGET_PATH": str(tmp_path / "dbt_target"), "DBT_LOG_PATH": str(tmp_path / "dbt_logs"),
           "PYTHONIOENCODING": "utf-8"}
    py = sys.executable
    dbt = [py, "-m", "dbt.cli.main", "build", "--project-dir", str(DBT), "--profiles-dir", str(DBT),
           "--exclude", "tag:gate"]
    _run(dbt, env)  # baseline on the current code
    before = _closed_sums(tmp_path / "warehouse.duckdb")
    _run([py, "-m", "generator", "--append-day", "1"], env)
    _run([py, "-m", "ingest.feed"], env)
    _run([py, "-m", "ingest"], env)
    _run(dbt, env)
    after = _closed_sums(tmp_path / "warehouse.duckdb")
    # a period that was closed before the new day must carry exactly the same earned total
    changed = {k: (v, after.get(k)) for k, v in before.items() if after.get(k) is None or abs(after[k] - v) > 0.01}
    assert not changed, f"closed periods restated: {dict(list(changed.items())[:10])}"
