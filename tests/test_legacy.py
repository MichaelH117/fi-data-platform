"""The legacy database (docs/V3_DESIGN.md 8.1): offline checks always; a real build when Postgres is up.

The Postgres tests build into throwaway databases (``vsc_legacy_pytest`` / ``vsc_legacy_admin_pytest``)
from the warehouse at WAREHOUSE_PATH and drop them afterwards. They skip when the server is down or
no warehouse is built (CI has neither).
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

import pandas as pd
import pytest

from legacy import DDL_DIR, SEMANTIC_DIR, databases
from legacy import build as lb
from legacy import semantic as ls

ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))
TEST_DB, TEST_ADMIN_DB = "vsc_legacy_pytest", "vsc_legacy_admin_pytest"


# ---------------------------------------------------------------------- offline
def _all_sql() -> str:
    return "\n".join([*(p.read_text(encoding="utf-8") for p in sorted(DDL_DIR.glob("*.sql"))),
                      *(sql for _, sql in lb.staging_sql())])


def test_legacy_sql_declares_no_keys_constraints_or_comments():
    sql = re.sub(r"--[^\n]*", "", _all_sql()).lower()
    for banned in ("primary key", "foreign key", "references ", "unique", "comment on", "constraint"):
        assert banned not in sql, banned


def test_indexes_only_on_contract_ids():
    cols = re.findall(r'create index on [\w."]+ \(([^)]*)\)', (DDL_DIR / "60_indexes.sql").read_text())
    assert cols and all(c.strip('"') in {"ContractId", "fxContractId"} for c in cols)
    assert "fxEarningDateId" not in (DDL_DIR / "60_indexes.sql").read_text()


def test_ddl_templates_render_with_every_parameter():
    params = lb.ddl_params(pd.Timestamp("2026-10-21").date())
    for f in lb.DDL_FILES:
        assert "${" not in lb.render(f, params), f


def test_semantic_models_are_generated_and_current():
    for rel, text in ls.render().items():
        p = SEMANTIC_DIR / rel
        assert p.exists(), f"{rel} missing: run python -m legacy semantic"
        assert p.read_text(encoding="utf-8") == text, f"{rel} is stale: run python -m legacy semantic"


def test_each_legacy_model_binds_exactly_one_schema_and_is_labelled():
    assert [m.name for m in ls.MODELS] == ["Ops", "Finance", "Reporting"]
    for m in ls.MODELS:
        d = SEMANTIC_DIR / f"{m.name}.SemanticModel" / "definition"
        assert ls.BANNER in (d / "model.tmdl").read_text(encoding="utf-8")
        schemas = set(re.findall(r'Schema="([^"]+)"', "".join(
            p.read_text(encoding="utf-8") for p in (d / "tables").glob("*.tmdl"))))
        assert schemas == {m.schema}
    assert "ANTI-PATTERN" in (SEMANTIC_DIR / "README.md").read_text(encoding="utf-8")


def test_legacy_models_conflict_on_measures_and_duplicate_dims():
    c = ls.conflicts()
    assert {"Claims Paid", "Coverage Cost", "Contracts"} <= set(c)
    assert len(ls.duplicated_dims()["contract"]) == 2


def test_database_names_follow_env():
    assert databases({}) == ("vsc_legacy", "vsc_legacy_admin")
    assert databases({"PG_LEGACY_DATABASE": "a", "PG_LEGACY_ADMIN_DATABASE": "b"}) == ("a", "b")


def test_cli_exits_2_when_postgres_is_down(monkeypatch, capsys):
    from legacy.__main__ import main
    from warehouse.postgres import sync

    monkeypatch.setattr(sync, "available", lambda settings=None: "server down")
    for cmd in ("build", "demo", "playbook", "status", "drop"):
        assert main([cmd]) == 2
    assert "not available" in capsys.readouterr().err


def test_playbook_row_checks_and_benchmark_helpers():
    from legacy import benchmark, playbook

    assert playbook.check(5, None) and playbook.check(5, 5) and playbook.check(5, [1, 9])
    assert not playbook.check(5, 4) and not playbook.check(None, None) and playbook.check(5, [None, 5])
    sql = "-- database: legacy_admin\n-- timeout: 5s\nset statement_timeout = '5s';\nselect 1;\n"
    assert benchmark.headers(sql) == {"database": "legacy_admin", "timeout": "5s"}
    assert benchmark.last_statement(sql) == "select 1"


# ---------------------------------------------------------------------- real build (Postgres)
def _pg_reason() -> str | None:
    from warehouse.postgres.sync import available

    if not WAREHOUSE.exists():
        return f"no warehouse at {WAREHOUSE}"
    return available()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    if (why := _pg_reason()) is not None:
        pytest.skip(f"PostgreSQL legacy build unavailable: {why}")
    from warehouse.postgres.sync import Settings

    s = Settings.from_env()
    lb.STATE_DIR = tmp_path_factory.mktemp("legacy_state")
    t0 = time.perf_counter()
    manifest = lb.build(WAREHOUSE, s, database=TEST_DB, admin_database=TEST_ADMIN_DB)
    manifest["elapsed"] = time.perf_counter() - t0
    con = s.connect(database=TEST_DB, autocommit=True)
    admin = s.connect(database=TEST_ADMIN_DB, autocommit=True)
    yield {"m": manifest, "con": con, "admin": admin, "s": s}
    con.close()
    admin.close()
    with s.connect(database="postgres", autocommit=True) as pc:
        for db in (TEST_DB, TEST_ADMIN_DB):
            pc.execute(f'drop database if exists "{db}" with (force)')


def _one(con, sql: str, params=()):
    return con.execute(sql, params).fetchone()


@pytest.mark.warehouse
def test_build_is_fast_and_complete(built):
    assert built["m"]["elapsed"] < 600
    n = built["m"]["row_counts"]
    assert n["legacy_finance.cube_earned_disbursement"] > 1_000_000  # the "huge" series, at modest scale
    for t in ("legacy_ops.tbl_contract", "legacy_ops.tbl_claim", "legacy_finance.cube_disbursement",
              "tenant_b.tbl_contract", "admin_eav.contract_field"):
        assert n[t] > 0, t


@pytest.mark.warehouse
def test_no_keys_constraints_or_comments_anywhere(built):
    for con in (built["con"], built["admin"]):
        assert _one(con, """select count(*) from pg_catalog.pg_constraint k
                            join pg_catalog.pg_namespace n on n.oid = k.connamespace
                            where n.nspname not in ('pg_catalog', 'information_schema')""")[0] == 0
        assert _one(con, """select count(*) from pg_catalog.pg_description d
                            join pg_catalog.pg_class c on c.oid = d.objoid
                            join pg_catalog.pg_namespace n on n.oid = c.relnamespace
                            where n.nspname not in ('pg_catalog', 'information_schema')""")[0] == 0


@pytest.mark.warehouse
def test_views_over_views_and_quoted_camelcase(built):
    con = built["con"]
    deps = _one(con, """select count(*) from pg_catalog.pg_depend d
                        join pg_catalog.pg_rewrite r on r.oid = d.objid join pg_catalog.pg_class v on v.oid = r.ev_class
                        join pg_catalog.pg_class t on t.oid = d.refobjid
                        where v.relname = 'vw_dim_contracts' and t.relname = 'vw_contract_base'""")[0]
    assert deps > 0
    assert _one(con, """select count(*) from pg_catalog.pg_attribute a join pg_catalog.pg_class c on c.oid = a.attrelid
                        where c.relname = 'vw_dim_dealers' and a.attname = 'Dealer Name'
                          and c.relnamespace = 'legacy_ops'::regnamespace""")[0] == 1


@pytest.mark.warehouse
def test_duplicate_dealer_identities_split_one_dealers_book(built):
    con = built["con"]
    rows = con.execute("""select regexp_replace("Number", '^V', ''), count(*), bool_or("Status" = 'INACTIVE')
                          from legacy_ops.vw_dim_dealers group by 1 having count(*) > 1""").fetchall()
    assert rows and all(n == 3 and inactive for _, n, inactive in rows)
    number = rows[0][0]
    one_id, all_ids = _one(con, """select count(*) filter (where d."Number" = %s and d."Status" = 'ACTIVE'
                                                             and d."DealerId" < 100000),
                                          count(*) from legacy_ops.vw_dim_contracts c
                                   join legacy_ops.vw_dim_dealers d on d."DealerId" = c."fxDealerId"
                                   where regexp_replace(d."Number", '^V', '') = %s""", (number, number))
    assert 0 < one_id < all_ids  # filtering one id misses the variants


@pytest.mark.warehouse
def test_stale_product_dim_drops_recent_contracts_on_inner_join(built):
    total, joined, latest = _one(built["con"], """
        select count(*), count(p."ProductId"),
               count(*) filter (where p."ProductId" is null and c."fxProductId" is not null)
        from legacy_ops.vw_dim_contracts c left join legacy_ops.vw_dim_products p on p."ProductId" = c."fxProductId"
    """)
    assert joined < total and latest > 0


@pytest.mark.warehouse
def test_claim_sign_flips_between_layers(built):
    ops, rep = _one(built["con"], """select (select sum("ClaimAmount") from legacy_ops.vw_dim_claims),
                                            (select sum("Claim Amount") from legacy_reporting.vw_fact_claims)""")
    assert ops < 0 < rep and abs(ops + rep) < 0.01


@pytest.mark.warehouse
def test_producer_vs_provider_filter_trap(built):
    name = _one(built["con"], 'select "Producer" from legacy_reporting.vw_dim_producers order by 1 limit 1')[0]
    wrong, right = _one(built["con"], """
        select (select count(*) from legacy_ops.vw_dim_contracts where "Producers" = %s),
               (select count(*) from legacy_reporting.vw_fact_contracts f
                join legacy_reporting.vw_dim_producers p on p."ProducerId" = f."fxProducerId" where p."Producer" = %s)
    """, (name, name))
    assert wrong == 0 < right


@pytest.mark.warehouse
def test_earnings_series_has_sentinel_and_no_date_index(built):
    con = built["con"]
    assert _one(con, """select count(*) from legacy_finance.cube_earned_disbursement
                        where "fxEarningDateId" = 19000101""")[0] > 0
    idx_cols = {r[0] for r in con.execute("""
        select a.attname from pg_catalog.pg_index i join pg_catalog.pg_class t on t.oid = i.indrelid
        join pg_catalog.pg_attribute a on a.attrelid = t.oid and a.attnum = any(i.indkey)
        where t.relname = 'cube_earned_disbursement'""").fetchall()}
    assert idx_cols == {"fxContractId"}


@pytest.mark.warehouse
def test_api_test_contracts_and_demo_dealers_are_present(built):
    con = built["con"]
    n, users, price = _one(con, """select count(*), array_agg(distinct "EnteredByUser"), max("VehiclePurchasePrice")
                                   from legacy_ops.vw_dim_contracts where "FullName" = 'TEST CUSTOMER'""")
    assert n > 0 and users == ["svc_api"] and price == 1
    assert _one(con, """select count(*) from legacy_ops.vw_dim_contracts c
                        join legacy_ops.dim_demo_dealers d on d."Dealer Id" = c."fxDealerId" """)[0] > 0


@pytest.mark.warehouse
def test_union_spine_contract_dim(built):
    con = built["con"]
    plan = "\n".join(r[0] for r in con.execute(
        'explain select * from legacy_reporting.vw_dim_contracts where "fxProducerId" = 1').fetchall())
    assert "CTE spine" in plan and "CTE cost" in plan
    spine, contracts = _one(con, """select (select count(*) from legacy_reporting.vw_dim_contracts),
                                           (select count(distinct "ContractId") from legacy_ops.tbl_contract)""")
    assert spine >= contracts


@pytest.mark.warehouse
def test_gen2_and_gen3_copies_disagree(built):
    a, b, c, d = _one(built["con"], """
        select (select sum("CoverageCost") from legacy_finance.vw_fact_contracts),
               (select sum("CoverageCost") from legacy_finance.vw_fact_contracts_gen2),
               (select sum("CustomerRefund") from legacy_finance.vw_fact_cancels),
               (select sum("RefundAmount") from legacy_finance.vw_fact_cancels_gen3)""")
    assert a != b and c > 0 > d


@pytest.mark.warehouse
def test_tenant_b_mirror_lags_and_drifted(built):
    con = built["con"]
    mirror, live = _one(con, """select (select count(*) from tenant_b.tbl_contract),
                                       (select count(*) from legacy_ops.tbl_contract where "fxAdminCompanyId" = 2)""")
    assert 0 < mirror < live
    cols = {r[0] for r in con.execute("""select a.attname from pg_catalog.pg_attribute a
        join pg_catalog.pg_class c on c.oid = a.attrelid join pg_catalog.pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'tenant_b' and c.relname = 'tbl_contract' and a.attnum > 0""").fetchall()}
    assert "ContractStatus" in cols and "CurrentContractStatus" not in cols and "fxProviderId" not in cols


@pytest.mark.warehouse
def test_clutter_frozen_empty_backup_and_locked_sandbox(built):
    con, s = built["con"], built["s"]
    assert _one(con, "select count(*) from report.tbl_contract_new")[0] == 0
    baks = [r[0] for r in con.execute("""select n.nspname || '.' || c.relname from pg_catalog.pg_class c
        join pg_catalog.pg_namespace n on n.oid = c.relnamespace where c.relname like '%%\\_bak\\_%%'""").fetchall()]
    assert any(b.startswith("legacy_ops.") for b in baks) and any(b.startswith("report.") for b in baks)
    if built["m"]["reader_granted"]:
        assert _one(con, "select has_schema_privilege(%s, 'zz_sandbox_analyst', 'USAGE')", (s.reader,))[0] is False
        assert _one(con, "select has_schema_privilege(%s, 'legacy_ops', 'USAGE')", (s.reader,))[0] is True


@pytest.mark.warehouse
def test_eav_lives_in_a_separate_database(built):
    admin, con = built["admin"], built["con"]
    pivot = admin.execute("""select "fxContractId", max(case when "fxFieldId" = 101 then "Value" end),
                                    max(case when "fxFieldId" = 105 then "Value" end)
                             from admin_eav.contract_field group by 1""").fetchall()
    assert pivot and all(make and year for _, make, year in pivot)
    assert _one(admin, "select count(*) from pg_catalog.pg_namespace where nspname = 'legacy_ops'")[0] == 0
    assert _one(con, "select count(*) from pg_catalog.pg_namespace where nspname = 'admin_eav'")[0] == 0


@pytest.mark.warehouse
def test_vin_text_join_matches_less_than_normalized(built):
    exact, norm = _one(built["con"], """
        with c as (select distinct "VIN" as v, upper(trim("VIN")) as n from legacy_ops.tbl_contract)
        select count(*) filter (where s."VIN" in (select v from c)),
               count(*) filter (where upper(trim(s."VIN")) in (select n from c))
        from legacy_ops.vw_dim_stock_number s""")
    assert exact < norm


@pytest.mark.warehouse
def test_semantic_spec_columns_exist_in_the_built_views(built):
    con = built["con"]
    for m in ls.MODELS:
        for t in m.tables:
            have = {r[0] for r in con.execute("""select a.attname from pg_catalog.pg_attribute a
                join pg_catalog.pg_class c on c.oid = a.attrelid
                join pg_catalog.pg_namespace n on n.oid = c.relnamespace
                where n.nspname = %s and c.relname = %s and a.attnum > 0""", (m.schema, t.source)).fetchall()}
            missing = {c for c, _ in t.columns} - have
            assert not missing, f"{m.schema}.{t.source}: {missing}"
