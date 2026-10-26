"""Synthetic sources: reproducibility, volume shape, economics, deliberate messiness, daily append."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from generator import catalog as cat
from generator import lifecycle as lc
from generator.build import build
from generator.daily import append_day

AS_OF = date(2026, 10, 21)
SMALL = {"n_legacy": 1500, "n_core": 3000}


def _build(root: Path, seed: int = 7) -> dict[str, int]:
    return build(root / "admin_core.db", root / "legacy_admin.db", root / "landing", seed=seed, as_of=AS_OF, **SMALL)


def _read(db: Path, sql: str) -> pd.DataFrame:
    with sqlite3.connect(db) as conn:
        return pd.read_sql(sql, conn)


@pytest.fixture(scope="module")
def src(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("src")
    _build(root)
    return root


def test_same_seed_same_data(src: Path, tmp_path: Path) -> None:
    _build(tmp_path)
    for db, tables in (("admin_core.db", ["contract", "claim_payment_line", "disbursement", "remit_batch"]),
                       ("legacy_admin.db", ["contracts", "claims", "cancellations"])):
        for t in tables:
            pd.testing.assert_frame_equal(_read(src / db, f'select * from "{t}"'), _read(tmp_path / db,
                                                                                        f'select * from "{t}"'))
    for folder in (src / "landing").iterdir():
        assert (folder / "_manifest.json").read_text() == (tmp_path / "landing" / folder.name / "_manifest.json"
                                                           ).read_text()


def test_different_seed_differs(src: Path, tmp_path: Path) -> None:
    _build(tmp_path, seed=8)
    sql = "select contract_number, coverage_cost from contract"
    assert not _read(src / "admin_core.db", sql).equals(_read(tmp_path / "admin_core.db", sql))


def test_all_states_present_and_weighted_to_big_states(src: Path) -> None:
    c = _read(src / "admin_core.db", "select reportable_state from contract")["reportable_state"]
    assert set(c) == set(cat.STATES) and len(cat.STATES) == 51
    share = c.isin(list(cat.BIG_STATES)).mean()
    assert 0.6 < share < 0.8


def test_contract_economics_tie_to_disbursements(src: Path) -> None:
    db = src / "admin_core.db"
    c = _read(db, "select contract_id, coverage_cost, purchase_price, status from contract").set_index("contract_id")
    d = _read(db, """select t.contract_id, sum(d.amount) amt from disbursement d join "transaction" t using (txn_id)
                     where t.txn_type = 'SALE' group by 1""").set_index("contract_id")["amt"]
    assert (c["coverage_cost"] - d.reindex(c.index)).abs().max() < 0.02
    assert (c["purchase_price"] >= c["coverage_cost"]).all()
    assert set(c["status"]) <= set(lc.CONTRACT_STATUSES)
    assert {"PENDING", "INFORCE", "CANCELLED", "VOIDED"} <= set(c["status"])


def test_voided_contracts_have_soft_deleted_sale_postings(src: Path) -> None:
    v = _read(src / "admin_core.db", """select t.removed_from_source from "transaction" t
               join contract c using (contract_id) where c.status = 'VOIDED' and t.txn_type = 'SALE'""")
    assert len(v) and (v["removed_from_source"] == 1).all()


def test_change_tracking_versions_are_global_and_unique(src: Path) -> None:
    with sqlite3.connect(src / "admin_core.db") as conn:
        from generator.admin_core import TABLES

        versions = pd.concat([pd.read_sql(f'select row_version from "{t}"', conn) for t in TABLES])
        top = conn.execute("select version from _ct_version").fetchone()[0]
    assert versions["row_version"].is_unique
    assert versions["row_version"].max() == top


def test_claims_shape(src: Path) -> None:
    db = src / "admin_core.db"
    n_contracts = len(_read(db, "select contract_id from contract")) + SMALL["n_legacy"]
    claims = _read(db, "select claim_id, status, contract_id, legacy_contract_ref from claim")
    legacy = _read(src / "legacy_admin.db", "select distinct claim_no from claims")
    assert 0.12 < (len(claims) + len(legacy)) / n_contracts < 0.4
    lines = _read(db, "select claim_id, paid_amount, pending_amount from claim_payment_line")
    assert 2.0 < len(lines) / len(claims) < 4.5
    assert (lines["paid_amount"] >= 0).all()
    closed = claims.loc[claims["status"].str.startswith("CLOSED") | (claims["status"] == "DENIED"), "claim_id"]
    outstanding = lines[lines["claim_id"].isin(closed)].groupby("claim_id")["pending_amount"].sum()
    assert outstanding.abs().max() < 0.05  # pending reserve is released when a claim closes
    assert (claims["contract_id"].isna() & claims["legacy_contract_ref"].notna()).any()  # legacy run-off claims
    missing = claims["contract_id"].isna() & claims["legacy_contract_ref"].isna()
    assert 0 < missing.mean() < 0.02  # claims entered without a contract key


def test_legacy_messiness_is_present(src: Path) -> None:
    db = src / "legacy_admin.db"
    claims = _read(db, "select claim_no, amount_requested, amount_paid from claims")
    assert claims["claim_no"].str.fullmatch(r"\d{7}").all()
    assert (claims["amount_requested"] < 0).all() and (claims["amount_paid"].dropna() <= 0).all()
    c = _read(db, "select * from contracts")
    assert c["legacy_contract_id"].duplicated().any()
    codes = c["dealer_code"].dropna()
    assert c["dealer_code"].isna().any() and (codes != codes.str.strip().str.upper()).any()
    assert (~c["vin"].fillna("").str.fullmatch(r"[A-HJ-NPR-Z0-9]{17}")).any()
    assert (c["producer"] != c["producer_code"]).any()
    assert (c["legacy_contract_id"] >= 99_990_000).any()
    products = _read(db, "select max(plan_year) y from products")["y"].iloc[0]
    assert products < c["plan_year"].max()  # stale product table


def test_converted_claims_share_a_number_across_keyspaces(src: Path) -> None:
    legacy = set(_read(src / "legacy_admin.db", "select claim_no from claims")["claim_no"].str.lstrip("0"))
    core = set(_read(src / "admin_core.db", "select claim_number from claim")["claim_number"])
    assert legacy & core


def test_demo_and_test_contracts_exist_by_design(src: Path) -> None:
    db = src / "admin_core.db"
    assert len(_read(db, "select * from dealer where is_demo = 1")) == cat.N_DEMO_DEALERS
    assert len(_read(db, "select * from contract where contract_number like '%-9999____'"))


def test_landing_drops_have_manifest_and_planned_defects(src: Path) -> None:
    folders = sorted((src / "landing").iterdir())
    assert len(folders) == cat.LANDING_DAYS
    m1 = json.loads((folders[0] / "_manifest.json").read_text())
    m2 = json.loads((folders[1] / "_manifest.json").read_text())
    assert m1["sequence_no"] == 1 and set(m1["files"]) == {"deals.csv", "remittance.csv", "econtract_events.jsonl"}
    from generator.landing import sha256

    assert m1["files"]["econtract_events.jsonl"]["sha256"] != sha256(folders[0] / "econtract_events.jsonl")
    rows = len(pd.read_csv(folders[1] / "remittance.csv"))
    assert m2["files"]["remittance.csv"]["row_count"] == rows + 1
    remit = pd.concat(pd.read_csv(f / "remittance.csv") for f in folders)
    assert remit["remit_batch_id"].str.startswith("RB-GHOST").any()  # remittance without a deal


def test_append_day_is_deterministic_and_versioned(src: Path, tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    for d in (a, b):
        shutil.copytree(src, d)
    before = _read(a / "admin_core.db", "select version from _ct_version")["version"].iloc[0]
    stats = append_day(a / "admin_core.db", a / "legacy_admin.db", a / "landing")
    append_day(b / "admin_core.db", b / "legacy_admin.db", b / "landing")
    assert stats["new_deals"] >= 0
    after = _read(a / "admin_core.db", "select version from _ct_version")["version"].iloc[0]
    assert after > before
    for t in ["claim", "claim_payment_line", "contract_status_history"]:
        pd.testing.assert_frame_equal(_read(a / "admin_core.db", f"select * from {t}"),
                                      _read(b / "admin_core.db", f"select * from {t}"))
    new = sorted((a / "landing").iterdir())[-1]
    assert new.name == "2026-10-22"
    assert json.loads((new / "_manifest.json").read_text())["sequence_no"] == cat.LANDING_DAYS + 1


# --------------------------------------------------------------------------- claim timeline realism
def test_no_source_event_is_dated_after_the_as_of_date(src: Path) -> None:
    """IBNR claims (loss <= as-of, reported later) must not leak into the sources before their report day."""
    asof = str(AS_OF)
    core = src / "admin_core.db"
    checks = {
        "claim.reported_at": "select max(reported_at) from claim",
        "claim.updated_at": "select max(updated_at) from claim",
        "claim_payment_line.posted_at": "select max(posted_at) from claim_payment_line",
        "claim_history.changed_at": "select max(changed_at) from claim_history",
        "claim_note.noted_at": "select max(noted_at) from claim_note",
        "transaction.posted_at": 'select max(posted_at) from "transaction"',
        "contract.entered_date": "select max(entered_date) from contract",
        "cancellation.cancel_date": "select max(cancel_date) from cancellation",
        "remit_batch.received_at": "select max(received_at) from remit_batch",
        "cash_transaction.posted_at": "select max(posted_at) from cash_transaction",
    }
    for name, sql in checks.items():
        top = _read(core, sql).iloc[0, 0]
        assert top is None or str(top)[:10] <= asof, (name, top)
    leg = _read(src / "legacy_admin.db", "select max(reported_date) r, max(paid_date) p from claims").iloc[0]
    assert str(leg["r"]) <= asof and str(leg["p"]) <= asof


def test_ibnr_claims_are_held_back_in_the_plan(src: Path) -> None:
    held = _read(src / "admin_core.db", "select claim_id, claim_number, loss_day, reported_day "
                                        "from _gen_claim_plan where claim_id < 0")
    assert len(held) > 0
    assert (held["reported_day"] > str(AS_OF)).all() and (held["loss_day"] <= str(AS_OF)).all()
    assert held["claim_number"].isna().all()
    known = _read(src / "admin_core.db", "select count(*) n from _gen_claim_plan where claim_id > 0")["n"].iloc[0]
    claims = _read(src / "admin_core.db", "select count(*) n from claim")["n"].iloc[0]
    assert known == claims


def test_claim_lifecycle_has_a_realistic_long_tail(src: Path) -> None:
    from generator import claims as clm

    pl = clm.from_store(_read(src / "admin_core.db", "select * from _gen_claim_plan"))
    days = (pl["close_day"].to_numpy() - pl["reported_day"].to_numpy()).astype("m8[D]").astype(int)
    assert 0.03 < (days > 30).mean() < 0.15  # most claims settle in about three weeks; a few wait 30-120 days
    assert set(pl["hold"].dropna()) == {*clm.DECISION_HOLDS, clm.PAYMENT_HOLD}
    assert 0.02 < (pl["supp_amount"] > 0).mean() < 0.06
    lines = _read(src / "admin_core.db", "select paid_amount, authorized_change from claim_payment_line "
                                         "where line_type = 'SUPPLEMENT'")
    assert len(lines) and (lines["paid_amount"] > 0).all()
    assert (lines["paid_amount"] - lines["authorized_change"]).abs().max() < 0.005


def test_open_claim_inventory_can_age_past_30_days() -> None:
    """The claims KPI 'open claims aged 30+' is a real, non-zero share of the open inventory."""
    import numpy as np

    from generator import claims as clm

    n = 20_000
    rng = np.random.default_rng(1)
    par = pd.DataFrame({"subcat": "VSC-PT", "class": "A", "plan_year": cat.PLAN_YEARS[0], "ded": 100.0,
                        "coverage_limit": 10_000.0, "eff_day": np.datetime64("2024-01-01"), "tenant_id": 1,
                        "contract_id": np.arange(n), "legacy_contract_ref": None, "odometer": 30_000}, index=range(n))
    loss = np.datetime64("2026-01-01") + rng.integers(0, 290, n).astype("m8[D]")
    pl = clm.plan(par, loss, rng)
    asof = np.datetime64("2026-10-21")
    pl = pl[pl["reported_day"].to_numpy() <= asof]
    status, sub = clm.status_at(pl, asof)
    is_open = np.isin(status, ["OPEN", "APPROVED", "PENDING_PAYMENT"])
    age = (asof - pl["reported_day"].to_numpy()).astype("m8[D]").astype(int)
    assert 0.08 < (is_open & (age >= 30)).sum() / is_open.sum() < 0.30
    assert {"AWAITING_PARTS", "GOODWILL_REVIEW", "AWAITING_INVOICE"} <= set(sub[is_open])


def test_report_lag_model_is_shared_by_build_and_append_day() -> None:
    import inspect

    import numpy as np

    from generator import claims as clm
    from generator import daily, sim

    lag = sim.report_lag(50_000, np.random.default_rng(3))
    assert 0.04 < (lag >= 30).mean() < 0.06 and np.median(lag) <= 2
    assert "sim.report_lag" in inspect.getsource(clm.plan)
    assert "report_lag" not in inspect.getsource(daily._new_claims)  # append-day uses plan()'s default lag


def test_emergence_shape_is_product_aware() -> None:
    import numpy as np

    from generator import sim

    a, b = sim.emergence_shape(np.array(["NEW", "NEW", "NEW"]), np.array(["VSC-PT", "GAP", "PPM"]))
    mean = a / (a + b)
    assert mean[1] < 0.4 < mean[2] < 0.55 < mean[0]  # GAP front-loaded, maintenance ~uniform, new VSC back-loaded


def test_append_day_releases_held_claims_on_their_report_day(src: Path, tmp_path: Path) -> None:
    shutil.copytree(src, tmp_path / "a")
    db = tmp_path / "a" / "admin_core.db"
    sql = "select count(*) n from _gen_claim_plan where claim_id < 0 and reported_day <= '2026-10-22'"
    due = _read(db, sql)["n"].iloc[0]
    stats = append_day(db, tmp_path / "a" / "legacy_admin.db", tmp_path / "a" / "landing")
    assert stats["claims_released"] == due
    assert _read(db, sql)["n"].iloc[0] == 0
    c = _read(db, "select claim_id, claim_number, reported_at from claim")
    assert c["claim_id"].is_unique and c["claim_number"].is_unique and (c["claim_id"] > 0).all()
    assert c["reported_at"].max()[:10] <= "2026-10-22"


# --------------------------------------------------------------------------- synthetic business names
def test_business_names_never_use_person_names(src: Path) -> None:
    """Dealers, groups, repair shops, agents and users come from neutral word lists, not a name generator."""
    from faker.providers.person.en_US import Provider

    people = set(Provider.first_names) | set(Provider.last_names)
    assert not people & (set(cat.PLACE_WORDS_A) | set(cat.PLACE_WORDS_B))
    db = src / "admin_core.db"
    names = pd.concat([
        _read(db, "select dealer_name n from dealer where is_demo = 0")["n"],
        _read(db, "select group_name n from dealer_group")["n"],
        _read(db, "select holder_name n from account_holder where holder_type in ('REPAIR_FACILITY', 'AGENT')")["n"],
        _read(db, "select agent_name n from agent")["n"], _read(db, "select user_name n from admin_user")["n"],
        _read(db, "select entity_name n from participation_entity")["n"], _read(db, "select city n from dealer")["n"],
        _read(src / "legacy_admin.db", "select reinsurance_company n from dealers")["n"],
    ])
    words = {w.strip("()") for n in names for w in n.split()}
    assert not words & people, sorted(words & people)[:10]
    assert _read(db, "select dealer_name from dealer where is_demo = 0")["dealer_name"].is_unique


def test_participation_entity_names_keep_structure_codes_upper_case(src: Path) -> None:
    e = _read(src / "admin_core.db", "select entity_name, structure_type from participation_entity")
    for code in ("CFC", "NCFC", "PORC"):
        sub = e[e["structure_type"] == code]
        assert sub["entity_name"].str.endswith(f"Reinsurance Ltd ({code})").all()
    assert not e["entity_name"].str.contains(r"\b(?:Cfc|Ncfc|Porc|Mc[a-z])").any()


# --------------------------------------------------------------------------- CRM export + admin-API drops
def test_crm_export_and_admin_api_drop_are_written_and_reproducible(src: Path, tmp_path: Path) -> None:
    from generator import intake as itk

    crm = src / "intake" / "crm" / str(AS_OF)
    m = json.loads((crm / itk.MANIFEST).read_text())
    calls = pd.read_csv(crm / itk.CALLS_FILE, dtype=str)
    assert m["export_type"] == "FULL" and m["files"][itk.CALLS_FILE]["row_count"] == len(calls)
    assert list(calls.columns) == itk.CALL_COLS
    assert {"COMPLAINT", "CANCEL_REQUEST", "CLAIM_STATUS"} <= set(calls["reason"])
    assert calls["call_at"].str[:10].replace("/", "-", regex=False).min() >= str(itk.CRM_START)
    assert (calls["contract_number"].isna() & calls["phone_hash"].isna()).any()  # deliberate: no customer key
    assert calls["call_id"].duplicated().any()  # deliberate: duplicated call
    pages = sorted((src / "intake" / "admin_api" / str(AS_OF) / itk.API_ENTITY).glob("page_*.json"))
    body = json.loads(pages[0].read_text())
    assert body["page_count"] == len(pages) and body["total_records"] == cat.N_DEALERS + cat.N_DEMO_DEALERS
    assert sum(len(json.loads(p.read_text())["records"]) for p in pages) == body["total_records"]
    _build(tmp_path)
    assert (tmp_path / "intake" / "crm" / str(AS_OF) / itk.CALLS_FILE).read_bytes() == (crm / itk.CALLS_FILE
                                                                                         ).read_bytes()


# --------------------------------------------------------------------------- V3 review fixes
SUBCAT_SQL = """select cl.claim_id, cl.denied_reason, cl.odometer as claim_odometer, cl.loss_date, k.contract_id,
       k.effective_date, k.term_months, k.odometer, s.subcategory_code as subcat
from claim cl join contract k using (contract_id) join plan pl on pl.plan_id = k.plan_id
join product p on p.product_id = pl.product_id join product_subcategory s on s.subcategory_id = p.subcategory_id"""


def test_no_contract_stays_pending_past_the_auto_void_threshold(src: Path, tmp_path: Path) -> None:
    from generator import lifecycle as lc

    limit = str(pd.Timestamp(AS_OF) - pd.Timedelta(days=lc.PENDING_AUTO_VOID_DAYS))[:10]
    stale = _read(src / "admin_core.db", "select count(*) n from contract where status = 'PENDING' "
                                         f"and entered_date <= '{limit}'")["n"].iloc[0]
    assert stale == 0
    voided = _read(src / "admin_core.db", "select count(*) n from contract_status_history "
                                          "where to_status = 'VOIDED' and changed_at like '% 16:30:00'")["n"].iloc[0]
    assert voided > 0  # never-remitted contracts were auto-voided, not left pending
    shutil.copytree(src, tmp_path / "a")
    with sqlite3.connect(tmp_path / "a" / "admin_core.db") as conn:  # a stale pending row the daily sweep must void
        cid = conn.execute("select contract_id from contract where status = 'INFORCE' limit 1").fetchone()[0]
        conn.execute("update contract set status = 'PENDING', entered_date = '2026-01-02' where contract_id = ?",
                     [cid])
    stats = append_day(tmp_path / "a" / "admin_core.db", tmp_path / "a" / "legacy_admin.db", tmp_path / "a" / "landing")
    assert stats["auto_voided"] >= 1
    status = _read(tmp_path / "a" / "admin_core.db", f"select status from contract where contract_id = {cid}")
    assert status["status"].iloc[0] == "VOIDED"


def test_waiting_period_denials_have_losses_inside_the_wait(src: Path) -> None:
    c = _read(src / "admin_core.db", SUBCAT_SQL)
    c["age"] = (pd.to_datetime(c["loss_date"]) - pd.to_datetime(c["effective_date"])).dt.days
    c["wait"] = c["subcat"].map(lambda s: cat.SUBCAT_BY_CODE[s].wait_days)
    w = c[c["denied_reason"] == "WITHIN_WAITING_PERIOD"]
    assert len(w) > 0
    assert (w["age"] < w["wait"]).all()
    assert (c.loc[c["age"] < c["wait"], "denied_reason"] == "WITHIN_WAITING_PERIOD").all()


def test_mileage_is_per_vehicle_and_coverage_stops_at_the_mile_limit(src: Path) -> None:
    import numpy as np

    from generator import sim

    m1 = sim.daily_miles(["C1", "C2", "C1"], ["VSC-PT", "RV", "VSC-PT"])
    assert m1[0] == m1[2]  # drawn once per contract, reproducible from its key
    keys = [f"C{i}" for i in range(4000)]
    means = {s: sim.daily_miles(keys, [s] * len(keys)).mean() for s in ("VSC-PT", "RV", "PSP")}
    assert 34 < means["VSC-PT"] < 42 and 9 < means["RV"] < 15 and 3.5 < means["PSP"] < 6.5
    c = _read(src / "admin_core.db", SUBCAT_SQL)
    per_month = c["subcat"].map(lambda s: cat.SUBCAT_BY_CODE[s].miles_per_month)
    limited = per_month > 0
    driven = c["claim_odometer"] - c["odometer"]
    assert (driven[limited] <= (per_month * c["term_months"])[limited] + 1).all()
    age = (pd.to_datetime(c["loss_date"]) - pd.to_datetime(c["effective_date"])).dt.days.clip(lower=1)
    rate = (driven / age).groupby(c["subcat"]).median()
    assert rate.get("PSP", 0) < 10 and rate.get("RV", 0) < 20 and rate["VSC-PT"] > 25
    assert np.isfinite(rate).all()


def test_ancillary_and_limited_warranty_emerge_evenly() -> None:
    import numpy as np

    from generator import sim

    subs = np.array(["TW", "KEY", "WS", "LTW", "VSC-PT"])
    a, b = sim.emergence_shape(np.array(["NEW"] * len(subs)), subs)
    mean = a / (a + b)
    assert (np.abs(mean[:4] - 0.5) < 0.03).all() and mean[0] <= 0.5  # TW slightly early, never back-loaded
    assert mean[4] > 0.55  # new VSC keeps its factory-warranty back-loading


def test_some_postings_are_recorded_after_their_month_closed(src: Path) -> None:
    sql = ("select count(*) n from {t} where julianday(created_at) >= "
           "julianday(date(posted_at, 'start of month', '+1 month')) + 9")
    db = src / "admin_core.db"
    for t in ('"transaction"', "cash_transaction", "claim_payment_line"):
        n = _read(db, sql.format(t=t))["n"].iloc[0]
        total = _read(db, f"select count(*) n from {t}")["n"].iloc[0]
        assert 0 < n < 0.02 * total, t
    sale = _read(db, sql.format(t='"transaction"') + " and txn_type = 'SALE'")["n"].iloc[0]
    assert sale == 0
    bad = _read(db, "select count(*) n from claim_payment_line where updated_at < created_at")["n"].iloc[0]
    assert bad == 0 and str(_read(db, "select max(created_at) m from claim_payment_line")["m"].iloc[0]) <= \
        f"{AS_OF} 23:59:59"


def test_each_build_stamps_a_new_source_epoch(src: Path, tmp_path: Path) -> None:
    from generator import read_source_epoch

    core, leg = read_source_epoch(src / "admin_core.db"), read_source_epoch(src / "legacy_admin.db")
    assert core["seed"] == 7 and core["epoch"] == leg["epoch"]
    _build(tmp_path)
    again = read_source_epoch(tmp_path / "admin_core.db")
    assert again["seed"] == 7 and again["epoch"] != core["epoch"]  # same seed, new build: a new epoch
    assert read_source_epoch(tmp_path / "missing.db") is None


def test_surcharge_misrates_and_blank_rv_chassis_vins_exist(src: Path) -> None:
    """The dealer-ops controls (surcharge audit AMOUNT_MISMATCH, RV missing chassis VIN) need cases to find."""
    db = src / "admin_core.db"
    s = _read(db, "select contract_id, surcharge_code, amount from contract_surcharge")
    diff = s["amount"] - s["surcharge_code"].map(cat.SURCHARGES)
    off = diff[diff.abs() > 0.001]
    assert 0.002 < len(off) / len(s) < 0.02
    assert off.abs().between(25, 50).all() and (off > 0).any() and (off < 0).any()
    terms = _read(db, "select c.contract_id, c.coverage_cost - r.coverage_cost - coalesce(o.amt, 0) sur "
                      "from contract c join rate r using (rate_id) "
                      "left join (select contract_id, sum(amount) amt from contract_option group by 1) o "
                      "using (contract_id)")
    charged = s.groupby("contract_id")["amount"].sum()
    t = terms.set_index("contract_id")["sur"].reindex(charged.index)
    assert (t - charged).abs().max() < 0.02  # the mis-rated amount is what the dealer cost carries
    rv = _read(db, "select chassis_vin from contract where class_code = 'RV'")
    missing = rv["chassis_vin"].isna().mean()
    assert len(rv) and 0.005 < missing < 0.10


def test_late_remittance_is_behaviour_not_a_dealer_switch() -> None:
    """Each dealer slips now and then at its own rate, so late rates spread out instead of 0% vs 30%+."""
    import numpy as np

    from generator.lifecycle import remit_delay

    rng = np.random.default_rng(3)
    dealers = [f"D{1001 + i}" for i in range(150)]
    profiles = {d: {"remit_delay": (15, 60) if i % 7 == 0 else (0, 4)} for i, d in enumerate(dealers)}
    due = np.datetime64("2025-10-03") + np.arange(52) * 7  # one weekly batch a year
    dn = np.repeat(dealers, len(due))
    days = np.tile(due, len(dealers))
    draw = np.array([rng.integers(*profiles[d]["remit_delay"]) for d in dn])
    delay = remit_delay(dn, days, draw, profiles)
    again = remit_delay(dn, days, draw, profiles)
    assert (delay == again).all()  # deterministic for the same draws
    rate = pd.Series(delay >= 20).groupby(dn).mean()
    good = rate[[d for d in dealers if profiles[d]["remit_delay"][0] == 0]]
    slow = rate[[d for d in dealers if profiles[d]["remit_delay"][0] > 0]]
    assert ((rate > 0) & (rate < 0.3)).mean() >= 0.5
    assert good.max() < 0.3 and (good > 0).mean() > 0.8 and good.nunique() > 10
    assert slow.min() > 0.2 and slow.max() < 1.0  # slow dealers are often, not always, late
    batch = pd.DataFrame({"d": dn, "due": days, "late": delay >= 20})
    assert batch.groupby(["d", "due"])["late"].nunique().max() == 1  # a batch slips as a whole


def test_rv_custom_fields_are_an_eav_store(src: Path) -> None:
    """V5 (docs/V3_DESIGN.md 8.3): RV coach/chassis attributes live in an EAV table, values as text."""
    from generator import custom_fields as cf

    db = src / "admin_core.db"
    defs = _read(db, "select distinct field_id, field_name, data_type from custom_field_def")
    assert set(defs["field_id"]) == {fid for fid, _, _ in cf.FIELD_DEFS}
    eav = _read(db, "select v.contract_id, v.field_id, v.value_text, c.class_code, c.chassis_vin, c.vehicle_year "
                    "from contract_custom_field v join contract c using (contract_id)")
    assert len(eav) and (eav["class_code"] == cf.RV_CLASS).all()  # only specialty (RV) contracts carry fields
    rv = _read(db, "select contract_id, chassis_vin from contract where class_code = 'RV'")
    assert set(eav["contract_id"]) == set(rv["contract_id"])
    vin = eav[eav["field_id"] == cf.FIELD_ID["Chassis VIN"]]
    assert (vin["value_text"] == vin["chassis_vin"]).all()
    blank = set(rv.loc[rv["chassis_vin"].isna(), "contract_id"])  # a blank chassis VIN is a MISSING row
    assert not blank & set(vin["contract_id"])
    yr = eav[eav["field_id"] == cf.FIELD_ID["Model Year"]]
    assert (yr["value_text"] == yr["vehicle_year"].astype(str)).all()
    assert len(eav) == len(eav.drop_duplicates(["contract_id", "field_id"]))


def test_v6_status_set_has_eleven_reachable_statuses(src: Path) -> None:
    """V6 edit 14: every status is written through a documented transition; holds are open at the as-of."""
    assert len(lc.CONTRACT_STATUSES) == 11
    h = _read(src / "admin_core.db", "select from_status, to_status from contract_status_history")
    pairs = set(h.dropna().itertuples(index=False, name=None))
    allowed = {(f, t) for f, tos in lc.TRANSITIONS.items() for t in tos}
    assert pairs <= allowed, pairs - allowed
    reached = set(h["to_status"])
    assert {"SUSPENDED", "REINSTATEMENT_PENDING"} <= reached
    c = _read(src / "admin_core.db", "select status from contract")
    assert set(c["status"]) <= set(lc.CONTRACT_STATUSES)


def test_v6_program_type_flows_from_product_to_contract(src: Path) -> None:
    """V6 edit 3: program_type is on insurer, product and contract; a contract takes its product's value."""
    from generator.reference import PROGRAM_TYPES

    db = src / "admin_core.db"
    ins = _read(db, "select program_type from insurer")
    assert set(ins["program_type"]) <= set(PROGRAM_TYPES)
    m = _read(db, "select c.program_type c_pt, p.program_type p_pt from contract c join plan using (plan_id) "
                  "join product p using (product_id)")
    assert len(m) and (m["c_pt"] == m["p_pt"]).all() and m["c_pt"].notna().all()
    assert set(m["c_pt"]) == set(PROGRAM_TYPES)


def test_v6_contract_documents_one_live_contract_document_per_contract(src: Path) -> None:
    """V6 edit 4: 1..n documents per contract; the latest non-deleted CONTRACT document has a form code
    from the cutover year on, and none before it."""
    from generator.booking import FORM_CODE_FROM

    db = src / "admin_core.db"
    d = _read(db, "select * from contract_document")
    assert set(d["document_type"]) == {"CONTRACT", "CANCEL", "OTHER"}
    n = _read(db, "select count(*) n from contract").iloc[0, 0]
    live = d[(d["document_type"] == "CONTRACT") & (d["is_deleted"] == 0)]
    assert live["contract_id"].nunique() == n  # every contract has a live CONTRACT document
    assert (d["is_deleted"] == 1).any() and d["related_document_id"].notna().any()
    year = d["generated_at"].str[:4].astype(int)
    assert d.loc[year < FORM_CODE_FROM, "form_code"].isna().all()
    assert live.loc[year[live.index] >= FORM_CODE_FROM, "form_code"].notna().all()
    assert d["document_name"].str.match(r"^[A-Z]+_(STD|CXL|OTH) - \d{4}\.\d - [A-Z]+$").all()
    cx = _read(db, "select count(*) n from cancellation").iloc[0, 0]
    assert (d["document_type"] == "CANCEL").sum() == cx


def test_v6_cobuyer_is_in_the_feed_only(src: Path) -> None:
    """V6 edit 9: the network deals file carries a co-buyer on ~20-30% of deals; the admin contract never does."""
    from generator import admin_core as ac
    from generator.deals import COBUYER_COLS

    deals = pd.concat([pd.read_csv(f, dtype=str) for f in sorted((src / "landing").glob("*/deals.csv"))])
    assert set(COBUYER_COLS) <= set(deals.columns)
    assert deals["cobuyer_name"].notna().any() and deals["cobuyer_name"].isna().any()
    from generator.deals import cobuyers
    from generator.sim import NamePools

    pools = NamePools(first=np.array(["Ann", "Bo"], dtype=object), last=np.array(["Cole", "Dunn"], dtype=object))
    ids = [f"DL{k:09d}" for k in range(5000)]
    co = cobuyers(ids, ["Eck"] * 5000, ["Town"] * 5000, ["TX"] * 5000, ["75001"] * 5000, pools)
    assert 0.2 < pd.notna(co["cobuyer_name"]).mean() < 0.3  # the contract: ~20-30% of deals
    assert deals.loc[deals["cobuyer_name"].notna(), "cobuyer_zip"].str.fullmatch(r"\d{5}").all()
    assert not set(COBUYER_COLS) & set(ac.column_specs()["contract"])


def test_v6_reference_columns_runoff_and_product_names(src: Path) -> None:
    """V6 edits 13, 15, 16 and A (source side)."""
    from generator.reference import DNU_SUFFIX, RUNOFF_DATE

    db = src / "admin_core.db"
    pe = _read(db, "select * from participation_entity")
    assert set(pe["status"]) == {"ACTIVE", "RUNOFF"} and pe["cession_type"].str.len().eq(1).all()
    assert pe["gl_class_code"].notna().all()
    assert set(_read(db, "select status from reinsurer")["status"]) == {"ACTIVE", "RUNOFF"}
    late = _read(db, f"""select count(*) n from disbursement d join contract c using (contract_id)
        join participation_entity e on e.dealer_id = c.dealer_id
        join account_holder h on h.account_holder_id = d.account_holder_id
        where e.status = 'RUNOFF' and c.entered_date > '{RUNOFF_DATE}' and h.holder_type = 'DEALER_RESERVE'""")
    assert late.iloc[0, 0] == 0  # a run-off entity takes no contract entered after its run-off date
    p = _read(db, "select product_id, product_code, product_name from product")
    assert p["product_name"].str.match(r"^.+ \d{4} - [A-Z]+$").all()
    dnu = p[p["product_name"].str.endswith(f" - {DNU_SUFFIX}")]
    assert len(dnu) and dnu["product_code"].isin(p.loc[~p.index.isin(dnu.index), "product_code"]).all()
    plans = _read(db, "select distinct product_id from plan")
    assert not set(dnu["product_id"]) & set(plans["product_id"])  # a do-not-use product can never be sold
    r = _read(db, """select r.coverage_cost, sum(c.amount) parts from rate r join rate_component c using (rate_id)
                     group by r.rate_id, r.coverage_cost""")
    assert (r["coverage_cost"] - r["parts"]).abs().max() < 0.005  # rate - fees = reserve to the cent
    assert _read(db, "select count(*) from rate where coverage_level is null").iloc[0, 0] == 0
    c = _read(db, "select entered_date, finalized_at, in_service_date, is_esigned, claim_grace_days from contract")
    assert c["finalized_at"].notna().all() and (c["in_service_date"] <= c["entered_date"]).all()
    assert set(c["is_esigned"]) == {0, 1} and c["claim_grace_days"].notna().all()
