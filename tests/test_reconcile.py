"""Reconciliation gate: break detection and tolerances, plus the full source-vs-warehouse check."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import duckdb
import pytest

from pipeline import reconcile as rc

ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))

ENTITY = rc.Entity(
    "admin_core", "widgets", "amount",
    "select count(*), coalesce(sum(amount), 0) from widget",
    "select count(*), coalesce(sum(amount), 0) from raw_widget",
    "select count(*), coalesce(sum(amount), 0) from mart_widget",
    "select count(*), coalesce(sum(amount), 0) from excluded_widget",
)


def _sources(amounts: list[float]) -> dict[str, sqlite3.Connection]:
    src = sqlite3.connect(":memory:")
    src.execute("create table widget (amount real)")
    src.executemany("insert into widget values (?)", [(a,) for a in amounts])
    return {"admin_core": src}


def _warehouse(raw: list[float], mart: list[float], excluded: list[float]) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for name, vals in (("raw_widget", raw), ("mart_widget", mart), ("excluded_widget", excluded)):
        con.execute(f"create table {name} (amount double)")
        if vals:
            con.executemany(f"insert into {name} values (?)", [(v,) for v in vals])
    return con


def test_clean_flow_reconciles_with_documented_exclusions():
    breaks = rc.compare([ENTITY], _warehouse([10, 20, 30], [10, 20], [30]), _sources([10, 20, 30]))
    assert [b.comparison for b in breaks] == ["source -> raw", "raw -> marts"]
    assert all(b.ok for b in breaks)


def test_a_lost_row_is_a_break():
    breaks = rc.compare([ENTITY], _warehouse([10, 20, 30], [10, 20], []), _sources([10, 20, 30]))
    bad = [b for b in breaks if not b.ok]
    assert len(bad) == 1 and bad[0].comparison == "raw -> marts" and bad[0].row_diff == -1


def test_two_cents_is_a_break_and_one_cent_is_not():
    two = rc.compare([ENTITY], _warehouse([10.02], [10.02], []), _sources([10.0]))
    one = rc.compare([ENTITY], _warehouse([10.01], [10.01], []), _sources([10.0]))
    assert not two[0].ok and abs(two[0].amount_diff - 0.02) < 1e-9
    assert one[0].ok


def test_results_are_written_to_audit(tmp_path):
    breaks = rc.compare([ENTITY], _warehouse([1], [1], []), _sources([1]))
    with duckdb.connect(str(tmp_path / "wh.duckdb")) as con:
        rc.write_results(con, "RUN-R", breaks)
        rows = con.execute("select run_id, entity, comparison, status from audit.reconciliation "
                           "order by comparison desc").fetchall()
    assert rows == [("RUN-R", "widgets", "source -> raw", "pass"), ("RUN-R", "widgets", "raw -> marts", "pass")]


def test_entities_cover_both_sources_and_every_dollar_measure():
    covered = {(e.source_system, e.measure) for e in rc.ENTITIES}
    for measure in ("coverage_cost", "remitted cash", "claims paid", "customer refunds"):
        assert ("admin_core", measure) in covered
    assert {e.source_system for e in rc.ENTITIES} == {"admin_core", "legacy_admin", "crm", "admin_api"}


def test_roster_drift_and_call_contact_ties_are_reconciliation_gate_tests():
    recon = ROOT / "dbt" / "tests" / "reconciliation"
    drift = (recon / "recon_dealer_roster_drift.sql").read_text(encoding="utf-8")
    assert "int_dealer_roster_drift" in drift
    for kind in ("MISSING_IN_DIM", "MISSING_IN_API", "ATTRIBUTE_MISMATCH"):
        assert kind in drift
    assert (recon / "recon_call_contacts_tie.sql").exists()


def test_file_intakes_reconcile_against_the_delivered_rows_in_the_warehouse():
    """CRM and admin-API have no source DB: the delivered-row count (manifest / extract) is the source."""
    intake = {e.source_system: e for e in rc.ENTITIES if e.source_db == "warehouse"}
    assert set(intake) == {"crm", "admin_api"}
    assert "raw_crm._manifest" in intake["crm"].source_sql and "raw_crm._quarantine" in intake["crm"].quarantined_sql
    assert "raw_admin_api._extract" in intake["admin_api"].source_sql
    assert "raw_admin_api._quarantine" in intake["admin_api"].quarantined_sql


def test_warehouse_source_entity_runs_on_the_warehouse_connection():
    ent = rc.Entity("crm", "calls", "rows only", "select count(*), 0 from delivered",
                    "select count(*), 0 from raw_widget",
                    "select count(*), 0 from mart_widget", "select count(*), 0 from excluded_widget",
                    "select count(*), 0 from quarantined", source_db="warehouse")
    con = _warehouse([1, 2], [1], [2])
    con.execute("create table delivered as select * from range(3)")
    con.execute("create table quarantined as select * from range(1)")
    breaks = rc.compare([ent], con, {})  # no source DB connection needed
    assert [(b.comparison, b.expected_rows, b.actual_rows, b.ok) for b in breaks] == [
        ("source -> raw", 3, 3, True), ("raw -> marts", 2, 2, True)]
    con.execute("delete from quarantined")  # a delivered row that is neither in raw nor quarantined
    assert not rc.compare([ent], con, {})[0].ok


def test_claim_headers_are_reconciled_per_system_and_status_group():
    """Every claim header is tied, not only claims with payment lines; pending dollars too."""
    headers = {(e.source_system, e.entity) for e in rc.ENTITIES if e.measure == "requested amount"}
    for system in ("admin_core", "legacy_admin"):
        for g in rc.STATUS_GROUPS:
            assert any(s == system and f"({g}" in name for s, name in headers), (system, g)
    for e in rc.ENTITIES:
        if e.measure == "requested amount":
            assert "core.dim_claim" in e.mart_sql and "int_reportable_scope" in e.excluded_sql
    pending = [e for e in rc.ENTITIES if e.measure == "pending amount"]
    assert pending and "pending_amount" in pending[0].mart_sql


def _claim_world(dim_rows: list[tuple[str, str, float]]) -> tuple[duckdb.DuckDBPyConnection, dict]:
    """Admin Core source + staging: one OPEN, one DENIED, one CLOSED claim and one excluded (demo) claim."""
    claims = [(1, "OPEN", 100.0, "k1"), (2, "DENIED", 200.0, "k2"), (3, "CLOSED_FULLY_PAID", 300.0, "k3"),
              (4, "DENIED", 50.0, "demo")]
    src = sqlite3.connect(":memory:")
    src.execute("create table claim (claim_id int, status text, requested_amount real, removed_from_source int)")
    src.executemany("insert into claim values (?, ?, ?, 0)", [c[:3] for c in claims])
    con = duckdb.connect()
    for schema in ("staging", "core", "intermediate"):
        con.execute(f"create schema {schema}")
    con.execute("create table staging.stg_admin_core__claim (claim_id int, status varchar, "
                "requested_amount double, is_deleted boolean)")
    con.executemany("insert into staging.stg_admin_core__claim values (?, ?, ?, false)", [c[:3] for c in claims])
    con.execute("create table intermediate.int_claims_conformed (source_system varchar, status varchar, "
                "requested_amount double, contract_key varchar)")
    con.executemany("insert into intermediate.int_claims_conformed values ('admin_core', ?, ?, ?)",
                    [c[1:] for c in claims])
    con.execute("create table intermediate.int_reportable_scope as "
                "select * from (values ('k1', true), ('k2', true), ('k3', true), ('demo', false)) "
                "t(contract_key, is_reportable)")
    con.execute("create table core.dim_claim (source_system varchar, status_group varchar, requested_amount double)")
    con.executemany("insert into core.dim_claim values (?, ?, ?)", dim_rows)
    return con, {"admin_core": src}


def test_a_lost_denied_claim_without_payment_lines_breaks_its_own_bucket():
    entities = [e for e in rc.ENTITIES if e.source_system == "admin_core" and e.measure == "requested amount"]
    full = [("admin_core", "open", 100.0), ("admin_core", "denied", 200.0), ("admin_core", "paid", 300.0)]
    con, src = _claim_world(full)
    assert all(b.ok for b in rc.compare(entities, con, src))  # the demo claim is a documented exclusion
    con, src = _claim_world([r for r in full if r[1] != "denied"])  # a join drops the denied claim
    bad = [b for b in rc.compare(entities, con, src) if not b.ok]
    assert [(b.entity, b.comparison, b.row_diff, b.amount_diff) for b in bad] == [
        ("claim headers (denied)", "raw -> marts", -1, -200.0)]


# --------------------------------------------------------------------------- against a built warehouse
def _ready() -> bool:
    from generator import ADMIN_CORE_PATH, LEGACY_DB_PATH

    if not (WAREHOUSE.exists() and ADMIN_CORE_PATH.exists() and LEGACY_DB_PATH.exists()):
        return False
    try:
        with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
            con.execute("select 1 from core.fct_claim_payment_line limit 1")
        return True
    except duckdb.Error:
        return False

def _parallel_run_mart(rows):
    con = duckdb.connect(":memory:")
    con.execute("create schema core")
    con.execute("create table core.mart_migration_parallel_run (entity varchar, legacy_rows bigint, "
                "admin_core_rows bigint, excluded_rows bigint, legacy_amount double, admin_core_amount double, "
                "excluded_amount double, status varchar)")
    con.executemany("insert into core.mart_migration_parallel_run values (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return con


def test_parallel_run_ties_after_exclusions_and_flags_breaks():
    con = _parallel_run_mart([
        ("CLAIM_OPEN_AT_CUTOVER", 55, 55, 0, 1000.0, 950.0, 50.0, "TIED_AFTER_EXCLUSIONS"),
        ("CLAIM_CONTRACT_LINK", 55, 50, 4, 0.0, 0.0, 0.0, "BREAK"),
        ("CONTRACT_IN_FORCE_AT_CUTOVER", 100, 0, 0, 5000.0, 0.0, 0.0, "NOT_MIGRATED"),
    ])
    got = {b.entity: b for b in rc.parallel_run(con)}
    assert got["CLAIM_OPEN_AT_CUTOVER"].ok
    assert not got["CLAIM_CONTRACT_LINK"].ok
    assert got["CONTRACT_IN_FORCE_AT_CUTOVER"].ok and got["CONTRACT_IN_FORCE_AT_CUTOVER"].waiver == "not_migrated"


def test_parallel_run_is_skipped_when_the_mart_is_absent():
    assert rc.parallel_run(duckdb.connect(":memory:")) == []



@pytest.mark.skipif(not _ready(), reason="warehouse or source databases not built (run python -m pipeline.run)")
def test_source_raw_and_marts_reconcile_on_the_built_warehouse():
    from generator import ADMIN_CORE_PATH, LEGACY_DB_PATH

    sources = rc.open_sources(ADMIN_CORE_PATH, LEGACY_DB_PATH)
    try:
        with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
            breaks = rc.compare(rc.ENTITIES, con, sources)
    finally:
        for s in sources.values():
            s.close()
    assert [f"{b.source_system} {b.entity} {b.comparison}" for b in breaks if not b.ok] == []


def _has_marts() -> bool:
    try:
        with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
            con.execute("select participation_label from core.dim_dealer limit 1")
            con.execute("select 1 from finance.mart_cession_experience limit 1")
            con.execute("select 1 from intermediate.int_dealer_roster_drift limit 1")
        return True
    except duckdb.Error:
        return False


@pytest.mark.skipif(not (WAREHOUSE.exists() and _has_marts()), reason="warehouse marts not built")
def test_dealer_participation_agrees_with_finance_entities():
    """The dealer view names the same risk carrier finance reports, and never prints 'None'."""
    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        rows = con.execute("""
            select d.dealer_number, d.participation_entity_key, d.participation_entity_name, d.participation_label,
                   f.entity_name
            from core.dim_dealer d
            left join (select distinct participation_entity_key, dealer_key, entity_name
                       from finance.mart_cession_experience) f
              on f.participation_entity_key = d.participation_entity_key and f.dealer_key = d.dealer_key
            where d.dealer_key <> '-1'""").fetchall()
        finance_dealers = con.execute("select count(distinct dealer_key) from finance.mart_cession_experience"
                                      ).fetchone()[0]
    assert rows and all(label and "None" not in label for *_, label, _ in rows)
    with_part = [r for r in rows if r[1] is not None]
    assert len(with_part) == finance_dealers
    assert all(name == fin and label.startswith(name + ", ") for _, _, name, label, fin in with_part)
    assert all(label == "no dealer participation" for _, key, _, label, _ in rows if key is None)


@pytest.mark.skipif(not (WAREHOUSE.exists() and _has_marts()), reason="warehouse marts not built")
def test_dealer_roster_drift_classifies_every_api_dealer():
    """Every admin-API dealer is matched, explained (quarantined / demo / legacy) or a reconciliation-gate failure."""
    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        api = con.execute("select count(distinct upper(trim(dealer_number))) from raw_admin_api.dealer").fetchone()[0]
        kinds = dict(con.execute("select drift_type, count(*) from intermediate.int_dealer_roster_drift "
                                 "where on_api_roster or drift_type = 'API_RECORD_QUARANTINED' group by 1").fetchall())
        reportable = con.execute("select count(*) from core.dim_dealer where dealer_key <> '-1' "
                                 "and source_system = 'admin_core'").fetchone()[0]
    assert sum(kinds.values()) == api
    assert kinds.get("OK", 0) + kinds.get("API_RECORD_QUARANTINED", 0) == reportable
    assert not {"MISSING_IN_DIM", "MISSING_IN_API", "ATTRIBUTE_MISMATCH"} & set(kinds)
