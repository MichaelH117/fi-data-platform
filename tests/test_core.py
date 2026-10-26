"""Core warehouse layer, V6 round (docs/V3_DESIGN.md section 9): Data Engineering Manager edits.

Fast checks run on the repo alone (seeds, model SQL, the database-health module, the Operations
section). The ``needs_v6_warehouse`` checks run against a warehouse built with the V6 sources
(``WAREHOUSE_PATH``, e.g. a dev copy) and skip on one built before them.
"""

from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path

import duckdb
import pytest

from generator import catalog
from pipeline import db_health
from reports import ops

ROOT = Path(__file__).resolve().parents[1]
DBT = ROOT / "dbt"
WAREHOUSE = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))


def _seed(path: str) -> list[dict[str, str]]:
    with (DBT / "seeds" / path).open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _sql(name: str) -> str:
    return next(DBT.glob(f"models/**/{name}.sql")).read_text(encoding="utf-8")


# --------------------------------------------------------------------------- edit 6: obligor + family
def test_compliance_family_covers_every_subcategory_and_limited_warranty_is_not_vsc():
    fam = {r["subcategory_code"]: r for r in _seed("regulatory/compliance_product_family.csv")}
    assert set(fam) == {s.code for s in catalog.SUBCATEGORIES}
    assert fam["LTW"]["compliance_family"] == "ANCILLARY_OTHER" and fam["LTW"]["is_service_contract"] == "false"
    assert {r["compliance_family"] for r in fam.values()} <= {"VSC", "GAP", "MAINTENANCE", "ANCILLARY_OTHER"}
    assert all(fam[c]["compliance_family"] == "VSC" for c in ("VSC-PT", "VSC-SC", "VSC-EN", "VSC-EX"))


def test_dim_obligor_splits_internal_and_dealer_owned_in_separate_key_namespaces():
    sql = _sql("dim_obligor")
    assert "'internal_provider' as obligor_kind" in sql and "'dealer_owned'" in sql
    assert "md5('obligor|dealer_owned|'" in sql and "structure_type = 'DEALER_OWNED_OBLIGOR'" in sql


# --------------------------------------------------------------------------- edit 16: product names
def test_product_crosswalk_has_one_row_per_code_and_maps_every_legacy_code():
    rows = _seed("core/product_taxonomy_crosswalk.csv")
    codes = [r["product_code"] for r in rows]
    assert sorted(codes) == sorted({s.code for s in catalog.SUBCATEGORIES})
    assert len({r["short_code"] for r in rows}) == len(rows)
    legacy = {r["legacy_product_code"] for r in rows if r["legacy_product_code"]}
    assert legacy == set(catalog.LEGACY_SUBCATS)


@pytest.mark.parametrize(("name", "plan_year", "year", "suffix", "dnu", "base"), [
    ("Powertrain 2024 - FI", 2024, 2024, "FI", False, "Powertrain"),
    ("Ancillary Bundle 2019 - DNU", 2019, 2019, "DNU", True, "Ancillary Bundle"),
    ("GAP 2022 - AG", 2022, 2022, "AG", False, "GAP"),
    ("Tire & Wheel", 2021, 2021, None, False, "Tire & Wheel"),  # unsuffixed: year falls back to plan_year
])
def test_product_name_parse_rule(name, plan_year, year, suffix, dnu, base):
    """Runs the staging model's own expressions (minus the dbt macro) on one name."""
    sql = _sql("stg_admin_core__product")
    body = sql.split("parsed as (", 1)[1]
    body = body.replace("from p\n", "from p0\n", 1)
    with duckdb.connect() as con:
        con.execute("create table p0 as select ?::varchar as product_name, ?::bigint as plan_year", [name, plan_year])
        got = con.execute("with parsed as (" + body).fetchone()
        cols = [d[0] for d in con.description]
    row = dict(zip(cols, got, strict=True))
    assert (row["model_year"], row["program_suffix"], row["is_do_not_use"], row["product_base_name"]) == (
        year, suffix, dnu, base)


def test_no_do_not_use_product_test_exists_and_reads_dim_product():
    sql = (DBT / "tests" / "core" / "core_no_do_not_use_product_sold.sql").read_text(encoding="utf-8")
    assert "dim_product" in sql and "is_do_not_use" in sql


# --------------------------------------------------------------------------- edit 12: agent roll-up
def test_agent_hierarchy_maps_every_generated_territory():
    terr = {r["territory"]: r for r in _seed("core/agent_territory_hierarchy.csv")}
    from generator import reference
    src = Path(reference.__file__).read_text(encoding="utf-8") + Path(catalog.__file__).read_text(encoding="utf-8")
    for t in terr:
        assert f'"{t}"' in src, t  # every seeded territory is one the generator writes
    assert all(r["region"] for r in terr.values())


# --------------------------------------------------------------------------- edit 20: database health
def _layer_counts(rows: list[tuple[str, str, int]]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("create schema audit")
    con.execute("create table audit.layer_row_count (run_id varchar, layer varchar, schema_name varchar, "
                "relation_name varchar, resource_type varchar, materialized varchar, row_count bigint, "
                "counted_at timestamp)")
    for schema, name, n in rows:
        con.execute("insert into audit.layer_row_count values ('R1', 'marts', ?, ?, 'model', 'table', ?, now())",
                    [schema, name, n])
    return con


def test_zero_row_tables_flags_only_empty_serving_tables():
    con = _layer_counts([("core", "dim_a", 0), ("core", "dim_b", 5), ("staging", "stg_x", 0)])
    rows = db_health.zero_row_tables(con, "R1")
    assert [(r[0], r[2], r[3]) for r in rows] == [("zero_row_table", "core.dim_a", "warn")]
    con2 = _layer_counts([("finance", "mart_a", 3)])
    assert db_health.zero_row_tables(con2, "R1")[0][3] == "pass"


def test_stale_statistics_rule():
    now = datetime(2026, 1, 1)
    rows = db_health.stale_statistics([("core.a", 100, 50, now), ("core.b", 100, 5, now), ("core.c", 10, 0, None)])
    assert {r[2]: r[3] for r in rows} == {"core.a": "warn", "core.c": "warn"}
    assert db_health.stale_statistics([("core.b", 100, 5, now)])[0][3] == "pass"


def test_refresh_login_adhoc_ignores_refresh_clients():
    sessions = [("Microsoft Power BI Gateway", "10.0.0.5", "idle"), ("Mashup Engine", "local", "active"),
                ("psql", "local", "active"), ("", "10.0.0.9", "idle")]
    rows = db_health.refresh_login_adhoc(sessions, "bi_reader")
    assert [r[5] for r in rows] == ["psql from local (active)", "unnamed client from 10.0.0.9 (idle)"]
    assert db_health.refresh_login_adhoc(sessions[:2], "bi_reader")[0][3] == "pass"


def test_postgres_checks_skip_when_server_is_down():
    rows = db_health.postgres_rows(unavailable=lambda: "cannot connect to 127.0.0.1:5432")
    assert [(r[0], r[3]) for r in rows] == [("stale_statistics", "skipped"), ("refresh_login_adhoc", "skipped")]


def test_record_writes_audit_rows_and_ops_page_shows_them():
    con = _layer_counts([("core", "dim_a", 0)])
    n = db_health.record(con, "R1", pg_rows=lambda: [("stale_statistics", "postgres", "", "skipped", None, "down")])
    assert n == 2
    assert db_health.record(con, "R1", pg_rows=lambda: []) == 1  # rerun of a run id replaces its rows
    section = ops.db_health_section(con)
    assert section is not None and section.title == "Database health"
    assert section.rows[0][0] == "Empty serving table" and section.rows[0][2] == "core.dim_a"
    assert ops.db_health_section(duckdb.connect()) is None  # before the first run: no section


# --------------------------------------------------------------------------- against a V6 warehouse
def _has_v6() -> bool:
    if not WAREHOUSE.exists():
        return False
    try:
        with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
            con.execute("select form_code_source from core.dim_contract_terms limit 1")
            con.execute("select obligor_kind from core.dim_obligor limit 1")
        return True
    except duckdb.Error:
        return False


needs_v6_warehouse = pytest.mark.skipif(not _has_v6(), reason="warehouse not built with the V6 core models")


@needs_v6_warehouse
def test_v6_core_columns_on_the_built_warehouse():
    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        q = lambda sql: con.execute(sql).fetchall()  # noqa: E731
        kinds = dict(q("select obligor_kind, count(*) from core.dim_obligor group by 1"))
        assert kinds.get("internal_provider") and kinds.get("dealer_owned")
        assert q("select count(*) from core.dim_product where is_do_not_use") == [(0,)]
        assert q("select count(*) from core.dim_product where compliance_family = 'VSC' "
                 "and subcategory_code = 'LTW'") == [(0,)]
        assert q("select count(*) from core.dim_insurer "
                 "where insurer_key <> '-1' and program_type = 'UNKNOWN'") == [(0,)]
        # form code: every 2024+ Admin Core contract from a document; legacy never inferred
        assert q("select count(*) from core.dim_contract_terms t join intermediate.int_contracts_conformed k "
                 "using (contract_key) where k.source_system = 'admin_core' and k.entered_year >= 2024 "
                 "and t.form_code_source <> 'document'") == [(0,)]
        assert q("select count(*) from core.dim_contract_terms t join intermediate.int_contracts_conformed k "
                 "using (contract_key) where k.source_system = 'legacy_admin' and t.form_code_source <> 'missing'") \
            == [(0,)]
        # program fee routes to its own incentive group, never insurer premium
        assert q("select count(*) from core.fct_disbursement where disbursement_type = 'INSURER_FEE' and "
                 "program_type in ('AGENCY', 'PASS_THROUGH') and incentive_group <> 'PROGRAM_FEE'") == [(0,)]
        # entity x program type: ceded + retained = contracts; retained only after a run-off date
        assert q("select count(*) from core.dim_participation_entity_program where contracts_ceded + "
                 "contracts_retained <> contracts or (runoff_date is null and contracts_retained > 0)") == [(0,)]
        assert q("select count(*) from core.dim_agent where region is null or district is null") == [(0,)]
