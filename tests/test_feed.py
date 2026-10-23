"""Landing-feed intake: file checks, row checks, business rules, promotion, idempotency."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from generator.build import build
from generator.landing import FILES, MANIFEST, sha256
from ingest.contract import load_contract, validate
from ingest.feed import CONTRACTS, run

AS_OF = date(2026, 10, 21)


@pytest.fixture(scope="module")
def pristine(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("feed")
    build(root / "admin_core.db", root / "legacy_admin.db", root / "landing", seed=11, as_of=AS_OF,
          n_legacy=1000, n_core=3000)
    return root


@pytest.fixture
def src(pristine: Path, tmp_path: Path) -> Path:
    shutil.copytree(pristine, tmp_path / "s")
    return tmp_path / "s"


def _q(root: Path, sql: str, params=()) -> pd.DataFrame:
    with sqlite3.connect(root / "admin_core.db") as conn:
        return pd.read_sql(sql, conn, params=list(params))


def _drop(root: Path, day: str, seq: int, deals=None, remits=None, events=None, tamper: str | None = None) -> Path:
    """Write a hand-made landing folder with a correct manifest (optionally tampering one file's sha)."""
    folder = root / "landing" / day
    folder.mkdir(parents=True)
    deals = deals if deals is not None else pd.read_csv(next((root / "landing").iterdir()) / FILES["deals"],
                                                        dtype=str, nrows=0)
    remits = remits if remits is not None else pd.read_csv(next((root / "landing").iterdir()) / FILES["remittance"],
                                                           dtype=str, nrows=0)
    deals.to_csv(folder / FILES["deals"], index=False, lineterminator="\n")
    remits.to_csv(folder / FILES["remittance"], index=False, lineterminator="\n")
    (folder / FILES["econtract_events"]).write_text("".join(json.dumps(e) + "\n" for e in events or []))
    counts = {"deals": len(deals), "remittance": len(remits), "econtract_events": len(events or [])}
    files = {n: {"row_count": counts[k], "sha256": "0" * 64 if tamper == k else sha256(folder / n)}
             for k, n in FILES.items()}
    (folder / MANIFEST).write_text(json.dumps({"feed_date": day, "sequence_no": seq, "files": files}))
    return folder


def test_initial_drops_are_checked_and_promoted(src: Path) -> None:
    results = run(src / "admin_core.db", src / "landing")
    assert len(results) == 9
    status = {(r.feed_date, r.file_name): r.status for r in results}
    assert status[("2026-10-19", "econtract_events.jsonl")] == "REJECTED"
    assert status[("2026-10-20", "remittance.csv")] == "REJECTED"
    assert sum(s == "LOADED" for s in status.values()) == 7
    files = _q(src, "select feed_date, file_name, status, message from intake_file")
    assert len(files) == 9
    rej = _q(src, "select file_name, feed_date, reject_reason from intake_reject")
    n_bad = len(pd.read_json(src / "landing/2026-10-19/econtract_events.jsonl", lines=True))
    assert (rej["reject_reason"] == "checksum_mismatch").sum() == n_bad
    assert (rej["reject_reason"] == "row_count_mismatch").sum() == len(
        pd.read_csv(src / "landing/2026-10-20/remittance.csv"))
    assert {"unknown_contract", "duplicate_deal"} <= set(rej["reject_reason"].str.split(":").str[0])


def test_promoted_deals_become_pending_contracts_with_postings(src: Path) -> None:
    run(src / "admin_core.db", src / "landing")
    deals = pd.concat(pd.read_csv(f / FILES["deals"], dtype=str) for f in sorted((src / "landing").iterdir()))
    booked = _q(src, "select contract_id, deal_id, status from contract where deal_id in ({})".format(
        ",".join("?" * deals["deal_id"].nunique())), deals["deal_id"].unique())
    rejected = _q(src, "select raw_json from intake_reject where file_name = 'deals.csv'")
    rejected_ids = {json.loads(r)["deal_id"] for r in rejected["raw_json"]}
    assert len(booked) == deals["deal_id"].nunique() - len(rejected_ids - set(booked["deal_id"]))
    assert set(booked["status"]) <= {"PENDING", "INFORCE", "VOIDED"}
    disb = _q(src, """select t.contract_id, count(*) n from disbursement d join "transaction" t using (txn_id)
                      where t.txn_type = 'SALE' group by 1""").set_index("contract_id")["n"]
    assert (disb.loc[booked["contract_id"]] == 6).all()


def test_resent_remittance_is_promoted_next_day(src: Path) -> None:
    run(src / "admin_core.db", src / "landing")
    day2 = pd.read_csv(src / "landing/2026-10-20/remittance.csv", dtype=str)
    real = day2[~day2["remit_batch_id"].str.startswith("RB-GHOST")]
    if real.empty:
        pytest.skip("no real remittance on the rejected day")
    got = _q(src, "select c.contract_number, c.status from contract c join cash_transaction t using (contract_id)")
    assert set(real["contract_number"]) <= set(got["contract_number"])


def test_rerun_is_idempotent(src: Path) -> None:
    run(src / "admin_core.db", src / "landing")
    before = _q(src, "select count(*) n from contract")["n"].iloc[0]
    assert run(src / "admin_core.db", src / "landing") == []
    assert _q(src, "select count(*) n from contract")["n"].iloc[0] == before


def test_voids_duplicates_and_tampering(src: Path) -> None:
    run(src / "admin_core.db", src / "landing")
    pending = _q(src, "select contract_number from contract where status = 'PENDING' limit 1")["contract_number"][0]
    inforce = _q(src, "select contract_number from contract where status = 'INFORCE' limit 1")["contract_number"][0]
    ev = [{"event_id": "EVX1", "contract_number": pending, "event_type": "VOIDED", "event_at": "2026-10-22 15:00:00",
           "payload": {"reason": "test"}},
          {"event_id": "EVX2", "contract_number": inforce, "event_type": "VOIDED", "event_at": "2026-10-22 15:00:00",
           "payload": {"reason": "test"}},
          {"event_id": "EVX3", "contract_number": "VSC-00000000", "event_type": "BOGUS",
           "event_at": "2026-10-22 15:00:00", "payload": {}}]
    old = pd.read_csv(src / "landing/2026-10-21/remittance.csv", dtype=str)
    _drop(src, "2026-10-22", 4, remits=old, events=ev)
    res = {r.file_name: r for r in run(src / "admin_core.db", src / "landing")}
    assert res["remittance.csv"].status == "DUPLICATE_FILE"
    assert res["econtract_events.jsonl"].promoted == 1
    assert _q(src, "select status from contract where contract_number = ?", [pending])["status"][0] == "VOIDED"
    sale = _q(src, """select t.removed_from_source from "transaction" t join contract c using (contract_id)
                      where c.contract_number = ? and t.txn_type = 'SALE'""", [pending])
    assert (sale["removed_from_source"] == 1).all()
    reasons = set(_q(src, "select reject_reason from intake_reject where feed_date = '2026-10-22'")["reject_reason"])
    assert "void_after_remittance" in reasons
    assert any(r.startswith("not_accepted:event_type") for r in reasons)
    _drop(src, "2026-10-23", 5, tamper="deals")
    res = {r.file_name: r for r in run(src / "admin_core.db", src / "landing", only="2026-10-23")}
    assert res["deals.csv"].status == "REJECTED" and res["deals.csv"].message == "checksum_mismatch"


def test_deal_contract_quarantines_bad_vins_and_codes() -> None:
    tc = next(iter(load_contract(CONTRACTS["deals"]).values()))
    good = {c.name: None for c in tc.columns}
    good.update(deal_id="DL000000001", dealer_number="D1001", contract_number="VSC-00000001",
                vin="1HGCM82633A004352", sale_date="2026-10-20", product_code="VSC-PT", plan_code="VSC-PT-2026-D100",
                term_months="36", odometer="10", new_used="NEW", purchase_price="1999", coverage_cost="999.5")
    bad_vin = {**good, "deal_id": "DL000000002", "vin": "1HGCM82633A00435I"}
    bad_kind = {**good, "deal_id": "DL000000003", "new_used": "CPO"}
    res = validate(pd.DataFrame([good, bad_vin, bad_kind]), tc)
    assert list(res.row_reasons) == ["", "bad_pattern:vin", "not_accepted:new_used"]
