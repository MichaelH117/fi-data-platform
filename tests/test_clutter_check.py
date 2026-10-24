"""Schema registry + clutter check (docs/V3_DESIGN.md 8.4): rule engine, registry, live catalogs."""

from __future__ import annotations

import os
from pathlib import Path

import duckdb
import pytest

from pipeline import clutter_check as cc

ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))
ENTRIES = {
    "core": {"tier": "marts"}, "raw_x": {"tier": "raw"}, "lab": {"tier": "sandbox"}, "gold": {"tier": "compat"},
}


def _rules(f: list[cc.Finding]) -> set[tuple[str, str]]:
    return {(x.rule, f"{x.schema}.{x.object}" if x.object else x.schema) for x in f}


def test_registry_rows_are_complete():
    reg = cc.load_registry()
    assert cc.registry_problems(reg) == []
    assert set(reg["serving_tiers"]) <= set(cc.TIERS)
    # the serving schemas the Postgres sync publishes are all registered for Postgres
    from warehouse.postgres.sync import SCHEMAS

    assert set(SCHEMAS) | {"security"} <= set(reg["postgres"])


def test_registry_problems_flags_missing_fields_and_unknown_tier():
    reg = {"duckdb": {"a": {"owner": "x", "tier": "marts", "purpose": "p"},
                      "b": {"owner": "x", "tier": "lake", "purpose": "p", "retention": "r"}}, "postgres": {}}
    assert _rules(cc.registry_problems(reg)) == {("registry_incomplete", "a"), ("registry_incomplete", "b")}


@pytest.mark.parametrize("name", ["tbl_dealer_bak_20250101", "fct_claim_old", "dim_x_tmp", "tbl_contract_new",
                                  "orders_backup", "cube_v2", "summary_20240131", "copy_of_dim"])
def test_backup_names_in_a_serving_tier_are_flagged(name):
    f = cc.check("duckdb", ["core"], [cc.Relation("core", name, "table", 5)], ENTRIES, ["marts"], [])
    assert ("backup_name", f"core.{name}") in _rules(f)


@pytest.mark.parametrize("name", ["dim_dealer", "fct_transaction", "mart_new_business", "dim_old_name_map"])
def test_normal_names_pass(name):
    assert cc.check("duckdb", ["core"], [cc.Relation("core", name, "table", 5)], ENTRIES, ["marts"], []) == []


def test_empty_tables_unless_allowed_and_views_never_counted():
    rels = [cc.Relation("core", "dim_a", "table", 0), cc.Relation("core", "mart_q", "table", 0),
            cc.Relation("core", "vw_a", "view", None)]
    f = cc.check("duckdb", ["core"], rels, ENTRIES, ["marts"], ["core.mart_q"])
    assert _rules(f) == {("empty_table", "core.dim_a")}


def test_personal_objects_sandbox_schemas_and_unregistered_schemas():
    rels = [cc.Relation("core", "zz_my_work", "table", 3), cc.Relation("lab", "anything", "table", 3),
            cc.Relation("zz_analyst", "summary", "table", 3), cc.Relation("raw_x", "tbl_bak", "table", 0)]
    f = cc.check("duckdb", ["core", "lab", "zz_analyst", "raw_x"], rels, ENTRIES, ["marts", "compat"], [])
    assert _rules(f) == {
        ("personal_object", "core.zz_my_work"), ("personal_object", "lab.anything"),
        ("personal_object", "zz_analyst.summary"), ("unregistered_schema", "zz_analyst"),
    }  # raw tier is not a serving tier: its names and empty tables are not clutter


def test_in_memory_duckdb_catalog_round_trip(tmp_path):
    db = tmp_path / "w.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("create schema core; create schema scratch")
        con.execute("create table core.dim_a as select 1 as k")
        con.execute("create table core.dim_a_bak as select 1 as k")
        con.execute("create table core.fct_empty (k int)")
        con.execute("create view core.v as select * from core.dim_a")
    reg = {"duckdb": {"core": {"owner": "o", "tier": "marts", "purpose": "p", "retention": "r"},
                      "main": {"owner": "o", "tier": "ops", "purpose": "p", "retention": "r"}},
           "postgres": {}, "serving_tiers": ["marts"], "allow_empty": []}
    f = cc.check_duckdb(db, reg)
    assert _rules(f) == {("backup_name", "core.dim_a_bak"), ("empty_table", "core.fct_empty"),
                         ("unregistered_schema", "scratch")}


def test_cli_exit_codes(tmp_path):
    assert cc.main(["--warehouse", str(tmp_path / "missing.duckdb")]) == 2


@pytest.mark.skipif(not WAREHOUSE.exists(), reason="no built warehouse (run python -m pipeline.run)")
def test_built_warehouse_is_registered_and_clean():
    findings = cc.check_duckdb(WAREHOUSE, cc.load_registry())
    assert findings == [], "\n".join(map(str, findings))


def _pg_reason() -> str | None:
    from warehouse.postgres.sync import available

    return available()


@pytest.mark.skipif(_pg_reason() is not None, reason="Postgres is not available")
def test_postgres_serving_database_is_registered_and_clean():
    findings, skipped = cc.check_postgres(cc.load_registry())
    assert skipped is None
    assert findings == [], "\n".join(map(str, findings))
