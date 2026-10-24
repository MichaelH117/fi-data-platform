"""V5 foundation fixes in the modern warehouse (docs/V3_DESIGN.md 8.4).

Static checks always run (no warehouse needed): declared keys and contracts in the dbt YAML, the Postgres
sync's key / foreign-key / BRIN rules, the runtime regression check. Warehouse checks run against a built
warehouse and skip when it (or a new model) is missing; the Postgres check skips when the server is down.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import duckdb
import pytest
import yaml

from pipeline import runtime_check as rc
from warehouse.postgres import sync

ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))
CORE_KEYS = ROOT / "dbt" / "models" / "marts" / "core" / "_core_keys.yml"
NO_CONTRACT = {"fct_contract_monthly", "fct_earned_disbursement", "dim_reporting_date"}
# the calendar spans years before the first posting period: an attribute there, not a foreign key
NOT_A_FOREIGN_KEY = {"dim_date.posting_period_key"}
DIMENSION_OF = {
    "contract_key": "dim_contract", "dealer_key": "dim_dealer", "product_key": "dim_product",
    "agent_key": "dim_agent", "posting_period_key": "dim_posting_period", "claim_key": "dim_claim",
    "account_holder_key": "dim_account_holder", "insurer_key": "dim_insurer",
    "selling_dealer_key": "dim_producer", "provider_entity_key": "dim_producer",
}


def _core_models() -> dict[str, dict]:
    return {m["name"]: m for m in yaml.safe_load(CORE_KEYS.read_text(encoding="utf-8"))["models"]}


# ---------------------------------------------------------------------- declared keys (static)
def test_every_keyed_core_model_has_an_enforced_contract_and_primary_key():
    models = _core_models()
    assert {"dim_contract", "dim_dealer", "dim_product", "dim_producer", "dim_dealer_crosswalk",
            "fct_transaction", "fct_disbursement"} <= set(models)
    for name, m in models.items():
        if name in NO_CONTRACT:
            continue
        assert (m.get("config") or {}).get("contract", {}).get("enforced") is True, name
        pk = [c for c in m.get("constraints", []) if c["type"] == "primary_key"]
        assert len(pk) == 1 and pk[0]["columns"], name
        missing = [c["name"] for c in m["columns"] if not c.get("data_type")]
        assert not missing, f"{name}: columns without data_type {missing}"


def test_every_foreign_key_column_has_a_relationships_test():
    gaps = []
    for name, m in _core_models().items():
        for col in m["columns"]:
            dim = DIMENSION_OF.get(col["name"])
            if dim is None or dim == name or f"{name}.{col['name']}" in NOT_A_FOREIGN_KEY:
                continue
            tests = col.get("data_tests", []) or []
            if not any(isinstance(t, dict) and "relationships" in t for t in tests):
                gaps.append(f"{name}.{col['name']}")
    assert not gaps, f"FK columns without a relationships test: {gaps}"


def test_sync_prefers_the_declared_primary_key_constraint():
    keys = sync.declared_keys()
    assert keys["mart_regulatory_line_values"] == ("line_value_key",)  # was a NULL-able 6-column combination
    assert keys["dim_contract"] == ("contract_key",)
    assert keys["fct_earned_disbursement"] == ("contract_key", "disbursement_type", "posting_period_key",
                                               "earned_month_key")


def test_sync_turns_relationships_tests_into_foreign_keys():
    fks = {(f.model, f.column, f.ref_model, f.ref_column) for f in sync.declared_foreign_keys()}
    assert ("fct_transaction", "dealer_key", "dim_dealer", "dealer_key") in fks
    assert ("fct_earned_disbursement", "posting_period_key", "dim_posting_period", "posting_period_key") in fks
    assert ("dim_contract", "provider_entity_key", "dim_producer", "producer_key") in fks
    assert len(fks) >= 60


def test_brin_only_on_big_facts_with_a_month_column():
    big = sync.Table("core", "fct_earned_disbursement", [("contract_key", "VARCHAR"), ("month_start", "DATE")])
    assert sync.brin_column(big, 8_000_000) == "month_start"
    assert sync.brin_column(big, 10_000) is None
    assert sync.brin_column(sync.Table("core", "dim_x", [("k", "VARCHAR")]), 9_000_000) is None


class _Cur:
    """Records SQL; fails VALIDATE for one constraint, like an orphan row would."""

    def __init__(self, fail: str = "") -> None:
        self.sql: list[str] = []
        self.fail = fail

    def execute(self, q, params=None):
        text = q if isinstance(q, str) else q.as_string(None)
        self.sql.append(text)
        if "VALIDATE CONSTRAINT" in text and self.fail and self.fail in text:
            raise RuntimeError("insert or update violates foreign key constraint")


def test_add_foreign_keys_validates_and_keeps_not_valid_on_orphans():
    pytest.importorskip("psycopg")
    dim = sync.Table("core", "dim_dealer", [("dealer_key", "VARCHAR")], ("dealer_key",))
    fct = sync.Table("core", "fct_a", [("a_key", "VARCHAR"), ("dealer_key", "VARCHAR"), ("product_key", "VARCHAR")],
                     ("a_key",))
    fks = [sync.ForeignKey("fct_a", "dealer_key", "dim_dealer", "dealer_key"),
           sync.ForeignKey("fct_a", "product_key", "dim_product", "product_key")]  # parent not synced
    cur = _Cur()
    out = sync.add_foreign_keys(cur, [dim, fct], fks)
    assert out["validated"] == ["fct_a.dealer_key -> dim_dealer.dealer_key"] and len(out["skipped"]) == 1
    assert any("NOT VALID" in s for s in cur.sql) and any("VALIDATE CONSTRAINT" in s for s in cur.sql)
    cur = _Cur(fail="fk_fct_a__dealer_key")
    out = sync.add_foreign_keys(cur, [dim, fct], fks[:1])
    assert out["not_valid"] == ["fct_a.dealer_key -> dim_dealer.dealer_key"]
    assert "ROLLBACK TO SAVEPOINT fk_validate" in cur.sql


# ---------------------------------------------------------------------- runtime regression (static)
def _summary(d: Path, run_id: str, steps: dict[str, float], status: str = "ok") -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{run_id}.json").write_text(json.dumps({"run_id": run_id, "status": status, "steps": [
        {"name": n, "status": "ok", "seconds": s} for n, s in steps.items()]}), encoding="utf-8")


def test_runtime_check_compares_like_with_like(tmp_path):
    _summary(tmp_path, "20260101T000000Z", {"dbt": 100, "publish": 50, "reports": 50})
    _summary(tmp_path, "20260102T000000Z", {"generate": 40, "dbt": 100, "publish": 50, "reports": 50})
    _summary(tmp_path, "20260103T000000Z", {"dbt": 110, "publish": 50, "reports": 50, "postgres": 900})
    latest, prev = rc.compare(rc.load_runs(tmp_path))
    assert latest.run_id == "20260103T000000Z" and prev.run_id == "20260101T000000Z"  # same steps; postgres ignored
    assert latest.seconds == 210 and rc.regression(latest, prev) == pytest.approx(0.05)
    assert rc.main(["--runs", str(tmp_path)]) == 0


def test_runtime_check_fails_on_a_regression_over_20_percent(tmp_path):
    _summary(tmp_path, "20260101T000000Z", {"dbt": 100, "reports": 100})
    _summary(tmp_path, "20260102T000000Z", {"dbt": 100, "reports": 200}, status="failed")  # ignored
    _summary(tmp_path, "20260103T000000Z", {"dbt": 150, "reports": 100})
    assert rc.main(["--runs", str(tmp_path)]) == 1


def test_recorded_runs_have_not_regressed():
    """The guard itself: the latest recorded default run is not >20% slower than the one before it."""
    latest, prev = rc.compare(rc.load_runs())
    if latest is None or prev is None:
        pytest.skip("fewer than two comparable recorded runs in data/runs")
    assert rc.regression(latest, prev) <= rc.MAX_REGRESSION, rc.report(latest, prev)


# ---------------------------------------------------------------------- built warehouse
def _con() -> duckdb.DuckDBPyConnection:
    if not WAREHOUSE.exists():
        pytest.skip("no built warehouse (run python -m pipeline.run)")
    return duckdb.connect(str(WAREHOUSE), read_only=True)


def _has(con, schema: str, table: str) -> bool:
    return bool(con.execute("select count(*) from duckdb_tables() where schema_name = ? and table_name = ?",
                            [schema, table]).fetchone()[0])


@pytest.fixture(scope="module")
def con():
    c = _con()
    if not _has(c, "core", "dim_dealer_crosswalk"):
        c.close()
        pytest.skip("warehouse predates the V5 foundation models (rebuild with python -m pipeline.run)")
    yield c
    c.close()


def test_duckdb_enforces_the_primary_keys(con):
    pk = {t for (t,) in con.execute("select table_name from duckdb_constraints() where schema_name = 'core' "
                                    "and constraint_type = 'PRIMARY KEY'").fetchall()}
    expected = {n for n in _core_models() if n not in NO_CONTRACT}
    assert expected <= pk, f"no PRIMARY KEY in DuckDB for {sorted(expected - pk)}"


def test_dealer_master_every_identity_resolves_to_one_surviving_dealer(con):
    rows = con.execute("""
        select count(*) filter (where d.dealer_key is null),
               count(*) filter (where x.is_survivor and x.source_dealer_number <> x.surviving_dealer_number),
               (select count(*) from core.dim_dealer) - (select count(*) from core.dim_dealer_crosswalk
                                                         where is_survivor)
        from core.dim_dealer_crosswalk as x left join core.dim_dealer as d using (dealer_key)""").fetchone()
    assert rows == (0, 0, 0)
    # every contract's dealer number resolves through the crosswalk to the dealer_key it carries
    bad = con.execute("""
        select count(*) from core.dim_contract as c
        join core.dim_dealer_crosswalk as x on x.source_dealer_number = c.dealer_number
        where c.dealer_key <> '-1' and x.dealer_key <> c.dealer_key""").fetchone()[0]
    assert bad == 0


def test_producer_roles_are_explicit(con):
    roles = dict(con.execute("select producer_role, count(*) from core.dim_producer group by 1").fetchall())
    assert roles.get("selling_dealer", 0) > 0 and roles.get("provider_entity", 0) > 0
    wrong = con.execute("""
        select count(*) filter (where s.producer_role not in ('selling_dealer', 'unknown')),
               count(*) filter (where p.producer_role not in ('provider_entity', 'unknown'))
        from core.dim_contract as c
        join core.dim_producer as s on s.producer_key = c.selling_dealer_key
        join core.dim_producer as p on p.producer_key = c.provider_entity_key""").fetchone()
    assert wrong == (0, 0)


def test_unknown_members_are_counted_not_dropped(con):
    share = con.execute("select max(unknown_share) from core.mart_unknown_member_share where is_gated").fetchone()[0]
    assert share is not None and share <= 0.02
    for dim, key in (("dim_contract", "contract_key"), ("dim_dealer", "dealer_key"), ("dim_product", "product_key")):
        assert con.execute(f"select count(*) from core.{dim} where {key} = '-1'").fetchone()[0] == 1, dim


def test_regulatory_line_values_have_a_real_key(con):
    n, keys, nulls = con.execute("select count(*), count(distinct line_value_key), count(*) - count(line_value_key) "
                                 "from regulatory.mart_regulatory_line_values").fetchone()
    assert n == keys and nulls == 0


def test_sentinel_dates_never_reach_core(con):
    hits = con.execute("""
        select count(*) from core.dim_contract
        where entered_date in (date '1900-01-01', date '1899-12-30')
           or effective_date in (date '1900-01-01', date '1899-12-30')
           or expiration_date in (date '1900-01-01', date '1899-12-30')""").fetchone()[0]
    assert hits == 0


def test_api_test_customers_are_not_reportable(con):
    leaked = con.execute("""
        select count(*) from staging.stg_admin_core__contract as s
        join core.dim_contract as c on c.source_system = 'admin_core'
                                   and c.source_contract_id = cast(s.contract_id as varchar)
        where upper(trim(concat_ws(' ', s.customer_first_name, s.customer_last_name))) = 'TEST CUSTOMER'""").fetchone()
    assert leaked[0] == 0


def test_eav_custom_fields_pivot_to_typed_columns(con):
    if not _has(con, "raw_admin_core", "contract_custom_field"):
        pytest.skip("raw EAV tables not ingested yet (generator V5 change)")
    types = dict(con.execute("select column_name, data_type from information_schema.columns "
                             "where table_schema = 'staging' and table_name = 'stg_admin_core__contract_custom_field'"
                             ).fetchall())
    assert types["model_year"] == "INTEGER" and types["coach_make"] == "VARCHAR"
    raw_contracts, pivoted, dupes = con.execute("""
        select (select count(distinct contract_id) from raw_admin_core.contract_custom_field),
               count(*), count(*) - count(distinct contract_id)
        from staging.stg_admin_core__contract_custom_field""").fetchone()
    assert pivoted == raw_contracts and dupes == 0
    assert con.execute("select count(*) from core.dim_contract_vehicle where has_custom_fields").fetchone()[0] > 0


# ---------------------------------------------------------------------- Postgres (opt-in)
def _pg():
    if sync.available() is not None:
        pytest.skip("Postgres is not available")
    return sync.Settings.from_env()


def test_postgres_copy_has_validated_foreign_keys_and_brin():
    s = _pg()
    with s.connect(autocommit=True) as pg, pg.cursor() as cur:
        cur.execute("select count(*) filter (where convalidated), count(*) from pg_constraint "
                    "where contype = 'f' and conname like 'fk\\_%'")
        validated, total = cur.fetchone()
        if total == 0:
            pytest.skip("the Postgres copy was synced before foreign keys existed (run warehouse.postgres sync)")
        assert validated == total
        cur.execute("select count(*) from pg_indexes where indexdef ilike '%using brin%'")
        assert cur.fetchone()[0] >= 1
