"""Raw-layer replication: CDC watermarks, audit columns, deltas, soft deletes, contracts, key diff, files."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

import duckdb
import pytest

from generator import admin_core as ac
from generator.build import build
from generator.daily import append_day
from ingest import ADMIN_CORE_CONTRACT, LEGACY_CONTRACT
from ingest.feed import run as run_feed
from ingest.load import run
from ingest.raw import AUDIT_COLS

AS_OF = date(2026, 10, 21)
ROOT = Path(__file__).resolve().parents[1]
LEGACY_TABLES = ["agents", "dealers", "products", "contracts", "claims", "cancellations"]


@pytest.fixture(scope="module")
def pristine(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("ing")
    build(root / "admin_core.db", root / "legacy_admin.db", root / "landing", seed=5, as_of=AS_OF,
          n_legacy=800, n_core=1500)
    run_feed(root / "admin_core.db", root / "landing")
    return root


@pytest.fixture
def src(pristine: Path, tmp_path: Path) -> Path:
    shutil.copytree(pristine, tmp_path / "s")
    return tmp_path / "s"


def _ingest(root: Path, **kw):
    res = run(root / "wh.duckdb", root / "admin_core.db", root / "legacy_admin.db", root / "landing",
              ADMIN_CORE_CONTRACT, LEGACY_CONTRACT, **kw)
    return {(r.source, r.table): r for r in res}


def _wh(root: Path, sql: str, params=()):
    with duckdb.connect(str(root / "wh.duckdb"), read_only=True) as con:
        return con.execute(sql, list(params)).fetchall()


def _src(db: Path, sql: str, params=()):
    with sqlite3.connect(db) as conn:
        return conn.execute(sql, list(params)).fetchall()


def test_first_load_copies_every_admin_core_row_with_audit_columns(src: Path) -> None:
    res = _ingest(src)
    assert all(r.status == "ok" for r in res.values())
    for t in ac.TABLES:
        n = _src(src / "admin_core.db", f'select count(*) from "{t}"')[0][0]
        assert res[("admin_core", t)].rows_loaded == n, t
        assert _wh(src, f'select count(*) from raw_admin_core."{t}"')[0][0] == n, t
    cols = {r[0] for r in _wh(src, "select column_name from information_schema.columns "
                                   "where table_schema = 'raw_admin_core' and table_name = 'contract'")}
    assert set(AUDIT_COLS) <= cols
    bad = _wh(src, "select count(*) from raw_admin_core.contract where _source_version <> row_version "
                   "or _loaded_by not like 'ingest@%' or _source_system <> 'admin_core' or _run_id is null")
    assert bad[0][0] == 0
    wm = _wh(src, "select watermark_value from audit.ingest_watermark where table_name = 'contract' "
                  "and source_system = 'admin_core'")[0][0]
    assert int(wm) == _src(src / "admin_core.db", "select max(row_version) from contract")[0][0]


def test_second_run_loads_nothing(src: Path) -> None:
    _ingest(src)
    assert all(r.rows_loaded == 0 for r in _ingest(src).values())


def test_append_day_replicates_only_new_versions(src: Path) -> None:
    _ingest(src)
    marks = dict(_wh(src, "select table_name, watermark_value from audit.ingest_watermark "
                          "where source_system = 'admin_core'"))
    append_day(src / "admin_core.db", src / "legacy_admin.db", src / "landing")
    run_feed(src / "admin_core.db", src / "landing")
    delta = _ingest(src)
    for t in ac.TABLES:
        expected = _src(src / "admin_core.db", f'select count(*) from "{t}" where row_version > ?',
                        [int(marks.get(t, 0))])[0][0]
        assert delta[("admin_core", t)].rows_read == expected, t
    assert delta[("admin_core", "contract")].rows_loaded > 0
    assert delta[("network_feed", "deals")].rows_loaded > 0
    runs = _wh(src, "select count(distinct _run_id) from raw_admin_core.contract")[0][0]
    assert runs == 2


def test_soft_delete_replicates_as_is_deleted(src: Path) -> None:
    _ingest(src)
    with sqlite3.connect(src / "admin_core.db") as conn:
        note = conn.execute("select note_id from claim_note where removed_from_source = 0 limit 1").fetchone()[0]
        ac.soft_delete(conn, "claim_note", [note], "2026-10-22 10:00:00")
    _ingest(src)
    rows = _wh(src, "select _is_deleted from raw_admin_core.claim_note where note_id = ? order by _source_version",
               [note])
    assert [r[0] for r in rows] == [False, True]



def test_admin_core_value_that_breaks_its_declared_type_is_quarantined_not_nulled(src: Path) -> None:
    db = src / "admin_core.db"
    cid, = _src(db, "select contract_id from contract order by contract_id limit 1")[0]
    with sqlite3.connect(db) as conn:  # SQLite accepts text in an INTEGER / DATE column
        conn.execute("update contract set term_months = 'twelve', entered_date = '2026-13-45' where contract_id = ?",
                     [cid])
    res = _ingest(src)
    r = res[("admin_core", "contract")]
    total = _src(db, "select count(*) from contract")[0][0]
    assert r.status == "ok" and r.rows_quarantined == 1 and r.rows_loaded == total - 1
    assert "type:term_months=1" in r.message and "type:entered_date=1" in r.message
    assert _wh(src, "select count(*) from raw_admin_core.contract where contract_id = ?", [cid])[0][0] == 0
    assert _wh(src, "select count(*) from raw_admin_core.contract where term_months is null")[0][0] == 0
    q = _wh(src, "select table_name, primary_key, reject_reason, raw_json from raw_admin_core._quarantine "
                 "order by table_name")
    assert [(t, k, why) for t, k, why, _ in q] == [("contract", str(cid), "type:entered_date;type:term_months")]
    assert '"twelve"' in q[0][3]
    logged = _wh(src, "select rows_quarantined from audit.ingest_run where source_system = 'admin_core' "
                      "and table_name = 'contract'")[0][0]
    assert logged == 1
    with sqlite3.connect(db) as conn:  # the source fixes the row: a new version replicates
        ac.update(conn, "contract", [cid], {"term_months": [36], "entered_date": ["2026-01-15"]}, "2026-10-22 09:00:00")
    assert _ingest(src)[("admin_core", "contract")].rows_loaded == 1
    assert _wh(src, "select term_months from raw_admin_core.contract where contract_id = ?", [cid]) == [(36,)]

def test_legacy_contract_quarantines_malformed_rows(src: Path) -> None:
    with sqlite3.connect(src / "legacy_admin.db") as conn:
        conn.execute("update contracts set sale_date = '13/45/2022' where rowid <= 3")
    res = _ingest(src)
    assert res[("legacy_admin", "contracts")].status == "ok"
    assert res[("legacy_admin", "contracts")].rows_quarantined >= 3
    assert _wh(src, "select count(*) from raw_legacy_admin._quarantine where reject_reason like '%sale_date%'"
               )[0][0] >= 3
    neg = _wh(src, "select count(*) from raw_legacy_admin.claims where amount_requested < 0")[0][0]
    assert neg == _wh(src, "select count(*) from raw_legacy_admin.claims")[0][0]  # raw keeps the legacy sign


def test_missing_required_column_rejects_batch_and_keeps_watermark(src: Path) -> None:
    with sqlite3.connect(src / "legacy_admin.db") as conn:
        conn.execute("alter table claims drop column loss_date")
    res = _ingest(src)
    assert res[("legacy_admin", "claims")].status == "error"
    assert res[("legacy_admin", "contracts")].status == "ok"
    assert _wh(src, "select count(*) from audit.ingest_watermark where table_name = 'claims'")[0][0] == 0


def test_legacy_hard_delete_becomes_a_tombstone(src: Path) -> None:
    _ingest(src)
    victim = _src(src / "legacy_admin.db", "select legacy_contract_id from contracts limit 1")[0][0]
    with sqlite3.connect(src / "legacy_admin.db") as conn:
        conn.execute("delete from contracts where legacy_contract_id = ?", [victim])
    res = _ingest(src)
    assert res[("legacy_admin", "contracts")].rows_deleted == 1
    last = _wh(src, "select _is_deleted from raw_legacy_admin.contracts where legacy_contract_id = ? "
                    "order by _source_version desc, _loaded_at desc limit 1", [victim])[0][0]
    assert last is True


def test_landing_files_manifest_and_quarantine_land_in_raw(src: Path) -> None:
    _ingest(src)
    deals = sum(1 for f in (src / "landing").iterdir() for _ in open(f / "deals.csv")) - 3  # minus headers
    assert _wh(src, "select count(*) from raw_network_feed.deals")[0][0] == deals
    bad = _wh(src, "select feed_date::varchar, file_name from raw_network_feed._manifest "
                   "where not checksum_ok or not row_count_ok order by 1")
    assert bad == [("2026-10-19", "econtract_events.jsonl"), ("2026-10-20", "remittance.csv")]
    rejects = _src(src / "admin_core.db", "select count(*) from intake_reject")[0][0]
    assert _wh(src, "select count(*) from raw_network_feed._quarantine")[0][0] == rejects
    meta = _wh(src, "select count(*) from raw_network_feed.deals where _source_file is null or _row_number is null "
                    "or _file_sha256 is null or _feed_date is null")[0][0]
    assert meta == 0



def _gap_diag(root: Path) -> list[str]:
    from ingest.network import drop_problems

    with duckdb.connect(str(root / "wh.duckdb"), read_only=True) as con:
        return drop_problems(con)


def test_freshness_gate_carries_the_lost_drop_rule() -> None:
    sql = (ROOT / "dbt" / "tests" / "diagnostics" / "diag_freshness.sql").read_text(encoding="utf-8")
    assert "source('audit', 'ingest_drop')" in sql and "select * from drop_gaps" in sql
    assert "select * from unloaded_drops" in sql


def test_late_or_repaired_feed_drop_still_loads_after_a_later_one(src: Path) -> None:
    folders = sorted(p for p in (src / "landing").iterdir() if p.is_dir())
    second = folders[1] / "_manifest.json"
    good = second.read_bytes()
    second.write_text("{not json", encoding="utf-8")  # drop 2 arrives broken; drop 3 still loads
    first = _ingest(src)
    assert first[("network_feed", "_manifest")].watermark_to == "3"
    seqs = {r[0] for r in _wh(src, "select distinct _source_version from raw_network_feed.deals")}
    assert seqs == {1, 3}
    assert _gap_diag(src) == ["landing drop not loaded: network_feed/" + folders[1].name,
                              "sequence gap: network_feed #2"]
    second.write_bytes(good)  # the producer repairs drop 2
    again = _ingest(src)
    assert again[("network_feed", "deals")].rows_loaded > 0
    expected = sum(1 for _ in open(folders[1] / "deals.csv")) - 1
    assert _wh(src, "select count(*) from raw_network_feed.deals where _source_version = 2")[0][0] == expected
    # staging (stg_network_feed__deals) = raw minus quarantined rows: drop 2's accepted rows now reach it
    staged = _wh(src, "select count(*) from raw_network_feed.deals f where _source_version = 2 and not exists ("
                      "select 1 from raw_network_feed._quarantine q where q._source_file = f._source_file "
                      "and (q._row_number = f._row_number or q._row_number is null or q._row_number = 0))")[0][0]
    assert staged > 0
    assert _gap_diag(src) == []
    assert all(r.rows_loaded == 0 for k, r in _ingest(src).items() if k[0] == "network_feed")


def test_drop_log_is_seeded_from_raw_for_a_warehouse_loaded_before_it_existed(src: Path) -> None:
    _ingest(src)
    with duckdb.connect(str(src / "wh.duckdb")) as con:
        con.execute("delete from audit.ingest_drop")
    res = _ingest(src)
    assert all(r.rows_loaded == 0 for k, r in res.items() if k[0] in ("network_feed", "crm", "admin_api"))
    assert _wh(src, "select count(*) from audit.ingest_drop where source_system = 'network_feed'")[0][0] == 3
    assert _gap_diag(src) == []

def test_full_refresh_reloads_everything(src: Path) -> None:
    _ingest(src)
    n = _wh(src, "select count(*) from raw_admin_core.contract")[0][0]
    _ingest(src, full_refresh=True)
    assert _wh(src, "select count(*) from raw_admin_core.contract")[0][0] == n
    assert _wh(src, "select count(distinct _run_id) from raw_admin_core.contract")[0][0] == 1


# --------------------------------------------------------------------------- CRM export + admin-API drops
def test_crm_and_admin_api_drops_land_with_lineage_and_quarantine(src: Path) -> None:
    import pandas as pd

    from generator.intake import phone_hash

    res = _ingest(src)
    calls_csv = src / "intake" / "crm" / str(AS_OF) / "calls.csv"
    n = len(pd.read_csv(calls_csv, dtype=str))
    assert res[("crm", "calls")].rows_loaded == n == _wh(src, "select count(*) from raw_crm.calls")[0][0]
    reasons = {r[0] for r in _wh(src, "select reject_reason from raw_crm._quarantine")}
    for expected in ("no_customer_key", "duplicate_call_id", "bad_timestamp:call_at", "bad_pattern:contract_number"):
        assert any(expected in r for r in reasons), expected
    q = _wh(src, "select count(*) from raw_crm._quarantine")[0][0]
    assert 0 < q < n * 0.1
    man = _wh(src, "select checksum_ok, row_count_ok, rows_valid, rows_quarantined, export_type from raw_crm._manifest")
    assert man == [(True, True, n - q, q, "FULL")]
    cols = {r[0] for r in _wh(src, "select column_name from information_schema.columns where table_schema = 'raw_crm' "
                                   "and table_name = 'calls'")}
    assert "phone_hash" in cols and not any("phone" in c and c != "phone_hash" for c in cols)  # no raw phone number
    meta = _wh(src, "select count(*) from raw_crm.calls where _source_file is null or _row_number is null "
                    "or _file_sha256 is null or _feed_date is null")[0][0]
    assert meta == 0
    # the phone hash bridges a call to its contract with the same rule any consumer can apply
    phones = dict(_src(src / "admin_core.db", "select contract_number, customer_phone from contract"))
    rows = [r for r in _wh(src, "select contract_number, phone_hash from raw_crm.calls "
                                "where contract_number is not null and phone_hash is not null") if r[0] in phones]
    assert rows and all(phone_hash(phones[c]) == h for c, h in rows)
    dealers = _src(src / "admin_core.db", "select count(*) from dealer")[0][0]
    assert _wh(src, "select count(*) from raw_admin_api.dealer")[0][0] == dealers
    assert _wh(src, "select reject_reason from raw_admin_api._quarantine") == [("bad_pattern:state",)]
    assert _wh(src, "select is_complete, pages_received, page_count from raw_admin_api._extract")[0][0] is True


def test_crm_file_with_a_bad_checksum_is_quarantined_whole(src: Path) -> None:
    path = src / "intake" / "crm" / str(AS_OF) / "calls.csv"
    path.write_text(path.read_text().replace("RESOLVED", "RESOLVED ", 1))
    _ingest(src)
    n = _wh(src, "select count(*) from raw_crm.calls")[0][0]
    assert _wh(src, "select count(*) from raw_crm._quarantine where reject_reason = 'checksum_mismatch'")[0][0] == n
    assert _wh(src, "select checksum_ok, rows_valid from raw_crm._manifest") == [(False, 0)]


def test_incomplete_admin_api_extract_is_rejected_and_deltas_load_once(src: Path) -> None:
    from generator.intake import api_pages_missing

    _ingest(src)
    for _ in range(3):  # extract 4 loses its last page in transit
        append_day(src / "admin_core.db", src / "legacy_admin.db", src / "landing")
    assert api_pages_missing(4)
    run_feed(src / "admin_core.db", src / "landing")
    delta = _ingest(src)
    assert delta[("crm", "_manifest")].rows_loaded == 3 and delta[("admin_api", "_extract")].rows_loaded == 3
    ext = _wh(src, "select sequence_no, is_complete, rows_valid, rows_quarantined, records_received "
                   "from raw_admin_api._extract order by 1")
    assert [e[1] for e in ext] == [True, True, True, False]
    assert ext[-1][2] == 0 and ext[-1][3] == ext[-1][4]
    assert _wh(src, "select count(*) from raw_admin_api._quarantine where reject_reason = 'incomplete_extract'"
               )[0][0] == ext[-1][4]
    types = _wh(src, "select export_type, count(*) from raw_crm._manifest group by 1 order by 1")
    assert types == [("DELTA", 3), ("FULL", 1)]
    again = _ingest(src)
    assert all(again[(s, t)].rows_loaded == 0 for s, t in again if s in ("crm", "admin_api"))


# --------------------------------------------------------------------------- end to end: append-day CDC + SCD2
SKIP_E2E = os.environ.get("WDP_SKIP_E2E") == "1"


def _step(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "WAREHOUSE_PATH": str(root / "wh.duckdb"), "ADMIN_CORE_PATH": str(root / "admin_core.db"),
           "LEGACY_DB_PATH": str(root / "legacy_admin.db"), "LANDING_DIR": str(root / "landing"),
           "INTAKE_DIR": str(root / "intake"), "DBT_TARGET_PATH": str(root / "dbt_target"),
           "DBT_LOG_PATH": str(root / "dbt_logs"), "DO_NOT_TRACK": "1", "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)


def _dbt_build(root: Path) -> None:
    out = _step(root, "-m", "dbt.cli.main", "build", "--project-dir", "dbt", "--profiles-dir", "dbt",
                "--exclude", "path:tests/claims", "path:tests/dealer", "path:tests/finance", "path:tests/regulatory")
    assert out.returncode == 0, out.stdout[-3000:]


@pytest.mark.skipif(SKIP_E2E, reason="end-to-end warehouse test (about 90 s) skipped by WDP_SKIP_E2E=1")
def test_append_day_end_to_end_cdc_idempotency_and_scd2(tmp_path: Path) -> None:
    """generate -> feed -> ingest -> dbt build, then --append-day 2 -> feed -> ingest -> dbt build, then the gates.

    Proves the definition-of-done item for the incremental run: CDC loads only the new versions,
    a second ingest loads nothing, the SCD2 snapshots close superseded versions, and the
    diagnostics and reconciliation gates pass on the incremental warehouse. The world is a seventh of
    the default size, big enough that two appended days change some contract and claim statuses.
    """
    src = tmp_path / "e2e"
    for args in (("-m", "generator", "--legacy-contracts", "3000", "--contracts", "6000"), ("-m", "ingest.feed")):
        assert _step(src, *args).returncode == 0
    _ingest(src)
    _dbt_build(src)
    marks = dict(_wh(src, "select table_name, watermark_value from audit.ingest_watermark "
                          "where source_system = 'admin_core'"))
    raw_before = {t: _wh(src, f'select count(*) from raw_admin_core."{t}"')[0][0] for t in ac.TABLES}
    assert _step(src, "-m", "generator", "--append-day", "2").returncode == 0
    assert _step(src, "-m", "ingest.feed").returncode == 0
    assert _step(src, "-m", "ingest").returncode == 0
    for t in ac.TABLES:  # raw rows added = source rows with a version above the old watermark
        new = _src(src / "admin_core.db", f'select count(*) from "{t}" where row_version > ?',
                   [int(marks.get(t, 0))])[0][0]
        assert _wh(src, f'select count(*) from raw_admin_core."{t}"')[0][0] - raw_before[t] == new, t
    assert all(r.rows_loaded == 0 for r in _ingest(src).values())  # idempotent
    _dbt_build(src)
    closed = {s: _wh(src, f"select count(*) from snapshots.{s} where dbt_valid_to is not null")[0][0]
              for s in ("snap_contract_status", "snap_claim_status")}
    assert all(v > 0 for v in closed.values()), closed
    for gate in ("pipeline.diagnostics", "pipeline.reconcile"):
        out = _step(src, "-m", gate)
        assert out.returncode == 0, (gate, out.stdout[-3000:], out.stderr[-3000:])


# --------------------------------------------------------------------------- watermark gap + source reset
def test_legacy_row_committed_in_the_watermark_second_is_not_missed(src: Path) -> None:
    db = src / "legacy_admin.db"
    with sqlite3.connect(db) as conn:
        conn.execute("update contracts set updated_at = '2026-10-21 23:00:00' where rowid = 1")
    first = _ingest(src)[("legacy_admin", "contracts")]
    assert first.watermark_to == "2026-10-21 23:00:00"
    with sqlite3.connect(db) as conn:  # committed after the pull, stamped in the same second as the watermark
        row = conn.execute("select * from contracts where rowid = 2").fetchone()
        cols = [d[1] for d in conn.execute("pragma table_info(contracts)")]
        new = dict(zip(cols, row, strict=True))
        new.update(legacy_contract_id=88_000_001, updated_at="2026-10-21 23:00:00")
        conn.execute(f"insert into contracts ({','.join(cols)}) values ({','.join('?' * len(cols))})",
                     list(new.values()))
    second = _ingest(src)[("legacy_admin", "contracts")]
    assert second.rows_loaded == 1
    assert _wh(src, "select count(*) from raw_legacy_admin.contracts where legacy_contract_id = 88000001")[0][0] == 1
    third = _ingest(src)[("legacy_admin", "contracts")]
    assert third.rows_read == 0 and third.rows_loaded == 0  # the look-back window never loads a row twice


def test_rebuilt_source_with_a_new_seed_is_detected_and_reloaded(src: Path) -> None:
    _ingest(src)
    build(src / "admin_core.db", src / "legacy_admin.db", src / "landing", seed=6, as_of=AS_OF,
          n_legacy=800, n_core=1500)
    run_feed(src / "admin_core.db", src / "landing")
    res = _ingest(src)
    assert {s for (s, t) in res if t == "_source_epoch"} >= {"admin_core", "legacy_admin", "network_feed"}
    for t in ac.TABLES:  # raw holds exactly the new world, not old + new rows
        n = _src(src / "admin_core.db", f'select count(*) from "{t}"')[0][0]
        assert _wh(src, f'select count(*) from raw_admin_core."{t}"')[0][0] == n, t
    n = _src(src / "legacy_admin.db", "select count(*) from contracts")[0][0]
    assert _wh(src, "select count(*) from raw_legacy_admin.contracts")[0][0] == n
    epoch = _src(src / "admin_core.db", "select epoch from _source_epoch")[0][0]
    stored = _wh(src, "select epoch, seed, reset_run_id is not null from audit.source_epoch "
                      "where source_system = 'admin_core'")[0]
    assert stored == (epoch, 6, True)
    assert all(t != "_source_epoch" for (_, t) in _ingest(src))  # the next run is incremental again


def test_admin_core_version_regression_without_epoch_forces_a_reload(src: Path) -> None:
    _ingest(src)
    with duckdb.connect(str(src / "wh.duckdb")) as con:  # an older warehouse: no epoch recorded yet
        con.execute("delete from audit.source_epoch")
        con.execute("update audit.ingest_watermark set watermark_value = '999999999' "
                    "where source_system = 'admin_core' and table_name = 'contract'")
    res = _ingest(src)
    assert ("admin_core", "_source_epoch") in res
    n = _src(src / "admin_core.db", "select count(*) from contract")[0][0]
    assert _wh(src, "select count(*) from raw_admin_core.contract")[0][0] == n
