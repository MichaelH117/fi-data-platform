"""Initial build: Admin Core (OLTP) history, the legacy admin system and the latest landing drops.

Timeline (defaults): the legacy book is sold 2019-2022, Admin Core takes over on 2023-01-01, and
the as-of date is the last day. The final ``LANDING_DAYS`` days of network activity (new deals,
received remittances, voids) are written as landing files instead of Admin Core rows, so the
first pipeline run has real files to validate and promote. Admin Core internal work (claims,
cancellations, expirations) is booked directly up to the as-of date.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
import warnings
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from generator import admin_core as ac
from generator import catalog as cat
from generator import claims as clm
from generator import custom_fields as cf
from generator import intake as itk
from generator import legacy as lg
from generator import lifecycle as lc
from generator import sim
from generator.booking import (
    Ids,
    Ref,
    amend_rows,
    cancel_rows,
    merge_rows,
    rate_deals,
    refund_terms,
    reinstate_rows,
    remit_rows,
    reverse_cancel_rows,
)
from generator.booking import sale_rows as book_sales
from generator.deals import make_deals
from generator.landing import contract_events, file_defects, inject_row_defects, write_drop
from generator.reference import LEGACY_TENANT, build_reference

DAY = sim.DAY
GEN_DDL = """
CREATE TABLE IF NOT EXISTS _gen_feed_plan (kind TEXT, contract_number TEXT, dealer_number TEXT, remit_batch_id TEXT,
    amount REAL, entered_day TEXT, sent_day TEXT, received_day TEXT, void_day TEXT, source TEXT);
CREATE TABLE IF NOT EXISTS _gen_claim_plan (claim_id INTEGER PRIMARY KEY, claim_number TEXT, tenant_id INTEGER,
    contract_id INTEGER, legacy_contract_ref INTEGER, subcat TEXT, loss_day TEXT, reported_day TEXT,
    decision_day TEXT, pp_day TEXT, close_day TEXT, inspected INTEGER, denied INTEGER, two_payments INTEGER,
    gross REAL, ded REAL, req REAL, cut REAL, trim REAL, auth REAL, paid_total REAL, denied_reason TEXT,
    payee INTEGER, adjuster INTEGER, hold TEXT, supp_day TEXT, supp_amount REAL);
"""


EPOCH_TABLE = "_source_epoch"


def new_epoch(seed: int) -> str:
    """Source epoch = seed + a build id. Every rebuild starts a new epoch (row versions restart), so
    ingest can tell a reset source from an incremental one even when the seed is unchanged."""
    return f"seed{seed}-{uuid.uuid4().hex[:12]}"


def write_epoch(conn: sqlite3.Connection, epoch: str, seed: int, as_of: date) -> None:
    conn.execute(f"CREATE TABLE IF NOT EXISTS {EPOCH_TABLE} (epoch TEXT NOT NULL, seed INTEGER NOT NULL, "
                 "as_of TEXT NOT NULL, built_at TEXT NOT NULL)")
    conn.execute(f"DELETE FROM {EPOCH_TABLE}")
    conn.execute(f"INSERT INTO {EPOCH_TABLE} VALUES (?, ?, ?, ?)",
                 [epoch, int(seed), str(as_of), datetime.now().strftime("%Y-%m-%d %H:%M:%S")])


def demo_api_key(seed: int, login: str) -> str:
    """Deterministic demo API key for a synthetic portal user (never a real secret)."""
    return hashlib.sha256(f"portal-demo:{seed}:{login}".encode()).hexdigest()[:24]


def save_state(conn: sqlite3.Connection, state: dict) -> None:
    conn.executemany("INSERT OR REPLACE INTO _gen_state (key, value) VALUES (?, ?)",
                     [(k, json.dumps(v, default=str)) for k, v in state.items()])


def load_state(conn: sqlite3.Connection) -> dict:
    return {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM _gen_state")}


def _concat(parts: list[pd.DataFrame]) -> pd.DataFrame:
    parts = [p for p in parts if p is not None and len(p)]
    if not parts:
        return pd.DataFrame()
    with warnings.catch_warnings():  # all-NA key columns (e.g. contract_id on legacy claims) are expected
        warnings.simplefilter("ignore", FutureWarning)
        return pd.concat(parts, ignore_index=True)


def assign_claim_parties(pl: pd.DataFrame, tables: dict, rng: np.random.Generator) -> pd.DataFrame:
    h = tables["account_holder"]
    shops = h.loc[h["holder_type"] == "REPAIR_FACILITY", "account_holder_id"].to_numpy()
    users = tables["admin_user"]
    adj = users[users["role"] == "ADJUSTER"].groupby("tenant_id")["admin_user_id"].apply(list).to_dict()
    pl = pl.copy()
    pl["payee"] = shops[rng.integers(0, len(shops), len(pl))]
    pl["adjuster"] = [adj[t][int(k) % len(adj[t])] for t, k in zip(pl["tenant_id"], rng.integers(0, 99, len(pl)),
                                                                     strict=True)]
    return pl


def _status_from_history(hist: pd.DataFrame) -> pd.DataFrame:
    h = hist.sort_values(["changed_at", "status_history_id"]).groupby("contract_id").tail(1)
    return h.set_index("contract_id")[["to_status", "changed_at"]]


# Late paperwork: a small seeded share of postings is recorded (created_at) after its event month has
# closed (business day 5 of the next month), so the period-lock rule has real rows to move into the
# first open period while the true event date (posted_at) is kept.
LATE_RECORD_SHARE = 0.004
LATE_RECORD_DAYS = (10, 40)  # days after the first of the following month (always past business day 5)


def late_recordings(tables_out: dict[str, pd.DataFrame], end: np.datetime64, rng: np.random.Generator) -> int:
    """Move ``created_at`` of a few non-sale transactions (with their disbursements and commissions), cash
    receipts and claim payment lines past the close of their posting month. Returns the rows moved."""
    moved = 0
    for name in ("transaction", "cash_transaction", "claim_payment_line"):
        df = tables_out.get(name)
        if df is None or not len(df):
            continue
        posted = sim.parse_days(df["posted_at"])
        rec = (posted.astype("M8[M]") + 1).astype("M8[D]") + rng.integers(*LATE_RECORD_DAYS, len(df)) * DAY
        ok = (rng.random(len(df)) < LATE_RECORD_SHARE) & (rec <= end)
        if "removed_from_source" in df:
            ok &= df["removed_from_source"].fillna(0).to_numpy() == 0
        if name == "transaction":
            ok &= (df["txn_type"] != "SALE").to_numpy()
        if not ok.any():
            continue
        ts = sim.stamp(rec[ok], rng)
        idx = df.index[ok]
        df.loc[idx, "created_at"] = ts
        df.loc[idx, "updated_at"] = np.where(df.loc[idx, "updated_at"].astype(str).to_numpy() > ts,
                                             df.loc[idx, "updated_at"].astype(str).to_numpy(), ts)
        moved += int(ok.sum())
        if name == "transaction":
            late = pd.Series(ts, index=df.loc[idx, "txn_id"].to_numpy())
            for child in ("disbursement", "commission"):
                c = tables_out.get(child)
                if c is None or not len(c):
                    continue
                m = c["txn_id"].isin(late.index).to_numpy()
                c.loc[m, "created_at"] = c.loc[m, "txn_id"].map(late).to_numpy()
                u, cr = c.loc[m, "updated_at"].astype(str).to_numpy(), c.loc[m, "created_at"].astype(str).to_numpy()
                c.loc[m, "updated_at"] = np.where(u > cr, u, cr)
    return moved


def build(
    core_path: Path,
    legacy_path: Path,
    landing_dir: Path,
    seed: int = cat.DEFAULT_SEED,
    as_of: date = cat.DEFAULT_AS_OF,
    n_legacy: int = cat.DEFAULT_LEGACY_CONTRACTS,
    n_core: int = cat.DEFAULT_CORE_CONTRACTS,
    landing_days: int = cat.LANDING_DAYS,
    intake_dir: Path | None = None,
) -> dict[str, int]:
    """(Re)create both source databases and the landing drops. Same seed + args => identical data."""
    rng = np.random.default_rng(seed)
    epoch = new_epoch(seed)
    pools = sim.NamePools.build(seed)
    created = "2018-12-01 09:00:00"
    end, cutover = np.datetime64(as_of), np.datetime64(cat.CUTOVER)
    cutoff = end - landing_days * DAY
    tables, profiles = build_reference(seed, created)
    tables["portal_user"]["api_key_hash"] = [hashlib.sha256(demo_api_key(seed, lg_).encode()).hexdigest()
                                             for lg_ in tables["portal_user"]["login"]]
    ref = Ref.from_tables(tables)
    dealers = tables["dealer"]
    agent_code = tables["agent"].set_index("agent_id")["agent_code"].to_dict()
    seq: dict[str, int] = {}

    # ------------------------------------------------------------------ legacy book
    leg_dealers = dealers[(dealers["tenant_id"] == LEGACY_TENANT)
                          & (sim.parse_days(dealers["onboarded_date"]) < cutover)]
    ldays = sim.sale_days(np.datetime64(cat.LEGACY_START), cutover - DAY, n_legacy, rng)
    ldeals = make_deals(ldays, leg_dealers, profiles, ref, pools, rng, seq, subcats=cat.LEGACY_SUBCATS)
    lrated, _ = rate_deals(ldeals, ref)
    lcon = lg.contracts(ldeals, lrated, ref.comp, agent_code, rng)
    lpol = lc.policies(ldeals, profiles)
    lpol = lc.with_mileage(lpol, [f"L{int(i)}" for i in lcon.loc[lpol.index, "legacy_contract_id"]])
    lpol["active_from"] = lpol["eff_day"]
    lcx = lc.sample_cancels(lpol, end, rng)
    lcx["legacy_contract_id"] = lcon.loc[lcx.index, "legacy_contract_id"].to_numpy()
    lcx["reason"] = lc.source_reason(lcx["reason"], lcx["legacy_contract_id"], "legacy")  # V7 edit 41
    terms_in = pd.DataFrame({
        "purchase_price": lcon.loc[lcx.index, "retail_price"].to_numpy(),
        "coverage_cost": lcon.loc[lcx.index, "dealer_cost"].to_numpy(),
        "admin_fee": lcon.loc[lcx.index, "admin_fee"].to_numpy(),
        "term_months": lcon.loc[lcx.index, "term_months"].to_numpy(),
        "contract_age_days": lcx["contract_age_days"].to_numpy(), "method": lcx["method"].to_numpy(),
        "withheld": 0.0}, index=lcx.index)
    lcx = lcx.join(refund_terms(terms_in))
    # legacy status at the as-of date (the legacy system stays the record for run-off status)
    lexp = lpol["exp_day"].to_numpy()
    lcon.loc[lexp <= end, "status"] = "expired"
    lcon.loc[lexp <= end, "updated_at"] = np.char.add(sim.iso(lexp[lexp <= end]), " 00:05:00")
    lcon.loc[lcx.index, "status"] = "cancelled"
    lcon.loc[lcx.index, "updated_at"] = lcx["processed_at"].to_numpy()
    lpol["end_day"] = lpol["exp_day"]
    lpol.loc[lcx.index, "end_day"] = lcx["cancel_day"].to_numpy()
    lpol["tenant_id"] = LEGACY_TENANT
    lpol["contract_id"] = None
    lpol["legacy_contract_ref"] = lcon["legacy_contract_id"].to_numpy()
    lclaims = clm.simulate(lpol, end, rng)

    # ------------------------------------------------------------------ Admin Core sales
    cdays = sim.sale_days(cutover, end, n_core, rng)
    cdeals = make_deals(cdays, dealers, profiles, ref, pools, rng, seq)
    test = np.sort(rng.choice(len(cdeals), max(5, len(cdeals) // 1000), replace=False))  # <prefix>-9999xxxx
    cdeals.loc[cdeals.index[test], "contract_number"] = [
        f"{n.split('-')[0]}-{cat.TEST_RANGE_START + k + 1}" for k, n in enumerate(cdeals["contract_number"].iloc[test])
    ]
    plan = lc.remit_plan(cdeals, dealers, profiles, rng)
    booked = cdeals["_sale_day"].to_numpy() <= cutoff
    denied_entry = booked & (rng.random(len(cdeals)) < 0.003) & (plan["kind"] == "REMIT").to_numpy()
    plan.loc[denied_entry, "kind"] = "DENIED"

    ids = Ids()
    acc: dict[str, list[pd.DataFrame]] = {}
    bd = cdeals[booked].copy()
    rated, reasons = rate_deals(bd, ref)
    assert (reasons == "").all()
    rated["_booked_at"] = sim.stamp(bd["_sale_day"].to_numpy(), rng)
    sale = book_sales(rated, ref, ids, end)
    merge_rows(acc, sale)
    contract = sale["contract"].set_index("contract_number", drop=False)
    cid_by_number = contract["contract_id"]

    # remittances booked in history (NETWORK up to the cutoff; PORTAL up to the as-of date)
    p = plan[booked].copy()
    p["contract_id"] = p["contract_number"].map(cid_by_number).to_numpy()
    p["tenant_id"] = p["contract_number"].map(contract["tenant_id"]).to_numpy()
    p["dealer_id"] = p["contract_number"].map(contract["dealer_id"]).to_numpy()
    is_remit = p["kind"] == "REMIT"
    horizon = np.where(p["source"] == "PORTAL", end, cutoff)
    got = is_remit & (p["received_day"].to_numpy() <= horizon)
    portal_open = is_remit & (p["source"] == "PORTAL") & ~got & (p["entered_day"].to_numpy() <= end)
    r = p[got | portal_open].copy()
    r["entered_at"] = np.char.add(sim.iso(r["entered_day"].to_numpy()), " 09:00:00")
    r["sent_at"] = np.where(r["sent_day"].to_numpy() <= end, np.char.add(sim.iso(r["sent_day"].to_numpy()),
                                                                          " 10:00:00"), None)
    r["received_at"] = np.where(got[got | portal_open], np.char.add(sim.iso(r["received_day"].to_numpy()),
                                                                    " 06:20:00"), None)
    rr = remit_rows(r, ids)
    merge_rows(acc, rr)
    received_day = pd.Series(r.loc[got[got | portal_open].to_numpy(), "received_day"].to_numpy(),
                             index=r.loc[got[got | portal_open].to_numpy(), "contract_id"].to_numpy())

    # voids, underwriting denials and auto-voids of never-remitted contracts booked in history:
    # status + soft-deleted sale postings
    gone = []
    entered = sim.parse_days(contract.loc[p["contract_number"], "entered_date"])
    auto_void_day = entered + lc.PENDING_AUTO_VOID_DAYS * DAY
    for kind, status in (("VOID", "VOIDED"), ("DENIED", "DENIED"), ("NONE", "VOIDED")):
        if kind == "VOID":
            m = (p["kind"] == kind).to_numpy() & (p["void_day"].to_numpy() <= cutoff)
        elif kind == "NONE":
            m = (p["kind"] == kind).to_numpy() & (auto_void_day <= end)
        else:
            m = (p["kind"] == kind).to_numpy()
        g = p[m]
        if kind == "VOID":
            at = np.char.add(sim.iso(g["void_day"].to_numpy()), " 15:00:00")
        elif kind == "NONE":
            at = np.char.add(sim.iso(auto_void_day[m]), " 16:30:00")
        else:
            at = np.char.add(sim.iso(entered[m] + DAY), " 16:00:00")
        merge_rows(acc, {"contract_status_history": pd.DataFrame({
            "status_history_id": ids.take("contract_status_history", len(g)), "tenant_id": g["tenant_id"].values,
            "contract_id": g["contract_id"].values, "changed_at": at, "from_status": "PENDING", "to_status": status,
            "created_at": at, "updated_at": at})})
        gone.append(pd.DataFrame({"contract_id": g["contract_id"].values, "at": at}))
    gone = _concat(gone).set_index("contract_id")["at"] if gone else pd.Series(dtype=object)

    # ------------------------------------------------------------------ Admin Core cancellations
    con = contract.set_index("contract_id", drop=False)
    live_idx = con.index[con.index.isin(received_day.index)]
    cd = bd.set_index(con["contract_id"].to_numpy()).loc[live_idx].rename_axis(None)
    cpol = lc.with_mileage(lc.policies(cd, profiles), [f"C{int(i)}" for i in live_idx])
    cpol["active_from"] = received_day.loc[live_idx].to_numpy()
    ccx = lc.sample_cancels(cpol, end, rng).rename_axis(None)
    ccx["contract_id"] = ccx.index.to_numpy()
    ccx["reason"] = lc.source_reason(ccx["reason"], ccx["contract_id"], "admin_core",
                                     con.loc[ccx["contract_id"], "tenant_id"].to_numpy())  # V7 edit 41
    cpol["end_day"] = cpol["exp_day"]
    cpol.loc[ccx.index, "end_day"] = ccx["cancel_day"].to_numpy()
    cpol["tenant_id"] = con.loc[live_idx, "tenant_id"].to_numpy()
    cpol["contract_id"] = live_idx.to_numpy()
    cpol["legacy_contract_ref"] = None
    cclaims = clm.simulate(cpol, end, rng)

    # ------------------------------------------------------------------ claims: number, split, emit
    lclaims = lclaims.sort_values("reported_day", kind="stable").reset_index(drop=True)
    lclaims["claim_number"] = (np.arange(1, len(lclaims) + 1)).astype(str)
    in_legacy = lclaims["reported_day"].to_numpy() < cutover
    status_cut, _ = clm.status_at(lclaims, cutover - DAY)
    converted = in_legacy & np.isin(status_cut, ["OPEN", "APPROVED", "PENDING_PAYMENT"])
    legacy_claims = lclaims[in_legacy]
    to_core_leg = lclaims[~in_legacy | converted].copy()
    missing = sim.pick(len(to_core_leg), 0.01, rng)  # claims entered without a contract key
    to_core_leg.iloc[missing, to_core_leg.columns.get_loc("legacy_contract_ref")] = None
    cclaims = cclaims.sort_values("reported_day", kind="stable").reset_index(drop=True)
    cclaims["claim_number"] = (np.arange(len(lclaims) + 1, len(lclaims) + len(cclaims) + 1)).astype(str)
    core_pl = _concat([to_core_leg, cclaims]).sort_values("reported_day", kind="stable")
    core_pl = core_pl.reset_index(drop=True)
    # True IBNR: losses on or before the as-of date that are reported after it. Their plans are kept
    # (negative claim_id, no claim number) and released by --append-day on the report day; nothing
    # about them reaches Admin Core before that.
    known = core_pl["reported_day"].to_numpy() <= end
    core_pl["claim_id"] = 0
    core_pl.loc[known, "claim_id"] = np.arange(1, int(known.sum()) + 1)
    core_pl.loc[~known, "claim_id"] = -np.arange(1, int((~known).sum()) + 1)
    core_pl.loc[~known, "claim_number"] = None
    core_pl = assign_claim_parties(core_pl, tables, rng)
    held_pl = core_pl[~known].copy()
    core_pl = core_pl[known].reset_index(drop=True)
    ev = clm.events(core_pl, None, end)
    hdr = clm.headers(core_pl, end, rng)
    hdr["updated_at"] = clm.last_event_ts(core_pl, end)
    comps = clm.components(core_pl, rng)
    for name, df in [("claim", hdr), ("claim_component", comps), *ev.items()]:
        df = df.copy()
        if ac.pk(name) not in df:
            df.insert(0, ac.pk(name), ids.take(name, len(df)))
        merge_rows(acc, {name: df})
    conv_ids = core_pl.loc[core_pl["claim_number"].isin(legacy_claims.loc[converted[in_legacy], "claim_number"]),
                           ["claim_id", "tenant_id", "reported_day"]]
    if len(conv_ids):
        at = np.char.add(sim.iso(np.full(len(conv_ids), cutover)), " 07:00:00")
        merge_rows(acc, {"claim_note": pd.DataFrame({
            "note_id": ids.take("claim_note", len(conv_ids)), "tenant_id": conv_ids["tenant_id"].values,
            "claim_id": conv_ids["claim_id"].values, "author_user_id": None, "noted_at": at,
            "note_text": clm.NOTES["CONVERT"], "created_at": at, "updated_at": at})})

    # cancellations on Admin Core contracts (claims paid before the cancel may be withheld)
    paid_before = []
    if len(ccx):
        cpl = core_pl[core_pl["contract_id"].notna()]
        j = cpl.merge(ccx[["contract_id", "cancel_day"]], on="contract_id")
        j = j[(j["close_day"] < j["cancel_day"]) & ~j["denied"].astype(bool)]
        paid_before = j.groupby("contract_id")["paid_total"].sum()
    withhold = rng.random(len(ccx)) < 0.3
    ccx["withheld"] = np.where(withhold, ccx["contract_id"].map(paid_before).fillna(0.0) if len(ccx) else 0.0, 0.0)
    ccx["from_status"] = "INFORCE"
    cx_rows = cancel_rows(ccx, con, ref, ids)
    merge_rows(acc, cx_rows)

    # reinstatements
    cancels = cx_rows.get("cancellation", pd.DataFrame())
    rx = pd.DataFrame()
    if len(cancels):
        pick = sim.pick(len(cancels), 0.04, rng)
        rx = cancels.iloc[pick][["cancel_id", "contract_id", "tenant_id", "coverage_cost_refund",
                                 "processed_at"]].copy()
        day = sim.parse_days(rx["processed_at"]) + rng.integers(5, 40, len(rx)) * DAY
        rx = rx[day <= end].copy()
        rx["reinstated_at"] = sim.stamp(day[day <= end], rng)
        rx["status"] = sim.weighted_choice(rng, ["APPROVED", "VOIDED", "DENIED"], [0.75, 0.15, 0.10], len(rx))
        ctx = cx_rows["transaction"].set_index("contract_id")["txn_id"]
        disb = cx_rows["disbursement"][cx_rows["disbursement"]["txn_id"].isin(ctx.loc[rx["contract_id"]])]
        comm = cx_rows["commission"][cx_rows["commission"]["txn_id"].isin(ctx.loc[rx["contract_id"]])]
        merge_rows(acc, reinstate_rows(rx, disb, comm, ids))

    # V7 edit 23: a few cancels (never a reinstated one, never one with nothing refunded) are undone in the
    # source by an offsetting cancel row. Own random stream, so no other generated value moves.
    if len(cancels):
        rrng = np.random.default_rng([seed, 23])
        pool = cancels[~cancels["cancel_id"].isin(rx["cancel_id"] if len(rx) else [])
                       & (cancels["customer_refund"] > 0)]
        undo = pool.iloc[np.flatnonzero(rrng.random(len(pool)) < cat.CANCEL_REVERSAL_RATE)]
        r_day = sim.parse_days(undo["processed_at"]) + rrng.integers(1, 11, len(undo)) * DAY
        undo = undo[r_day <= end]
        merge_rows(acc, reverse_cancel_rows(undo, cx_rows, sim.stamp(r_day[r_day <= end], rrng), ids))

    # amendments / renewals / transfers (+ a few transfers recorded against legacy contracts)
    alive = con.loc[live_idx]
    am_pick = sim.pick(len(alive), 0.03, rng)
    a = alive.iloc[am_pick]
    kind = sim.weighted_choice(rng, ["AMEND", "TRANSFER", "RENEWAL"], [0.65, 0.25, 0.10], len(a))
    a_day = received_day.loc[a["contract_id"]].to_numpy() + rng.integers(10, 120, len(a)) * DAY
    cancel_by = ccx.set_index("contract_id")["cancel_day"] if len(ccx) else pd.Series(dtype="M8[D]")
    limit = a["contract_id"].map(cancel_by).to_numpy().astype("M8[D]")
    ok = (a_day <= end) & (np.isnat(limit) | (a_day < limit))
    a, kind, a_day = a[ok], kind[ok], a_day[ok]
    delta = np.where(kind == "AMEND", np.round(rng.uniform(50, 300, len(a)), 2),
                     np.where(kind == "RENEWAL", np.round(a["coverage_cost"].to_numpy() * 0.5, 2), 0.0))
    am = pd.DataFrame({"contract_id": a["contract_id"].values, "legacy_contract_ref": None,
                       "tenant_id": a["tenant_id"].values, "amendment_type": kind, "effective_date": sim.iso(a_day),
                       "delta_coverage_cost": delta, "note": "Coverage change requested by dealer",
                       "at": sim.stamp(a_day, rng)})
    lalive = lcon[(lcon["status"] == "active") & (lcon["legacy_contract_id"] < lg.TEST_ID_START)]
    lt = lalive.sample(min(60, len(lalive)), random_state=int(rng.integers(1 << 31)))
    lt_day = cutover + rng.integers(1, int((end - cutover) / DAY), len(lt)) * DAY
    am = pd.concat([am, pd.DataFrame({
        "contract_id": None, "legacy_contract_ref": lt["legacy_contract_id"].values, "tenant_id": LEGACY_TENANT,
        "amendment_type": "TRANSFER", "effective_date": sim.iso(lt_day), "delta_coverage_cost": 0.0,
        "note": "Ownership transfer on a legacy contract", "at": sim.stamp(lt_day, rng)})], ignore_index=True)
    merge_rows(acc, amend_rows(am, con, ref, ids))

    # expirations, then final contract status from the history
    hist = _concat(acc["contract_status_history"])
    last = _status_from_history(hist)
    exp_day = sim.parse_days(con["expiration_date"])
    still = last.reindex(con.index)["to_status"].eq("INFORCE").to_numpy()
    ex = con[still & (exp_day <= end)]
    at = np.char.add(ex["expiration_date"].to_numpy().astype(str), " 00:05:00")
    merge_rows(acc, {"contract_status_history": pd.DataFrame({
        "status_history_id": ids.take("contract_status_history", len(ex)), "tenant_id": ex["tenant_id"].values,
        "contract_id": ex["contract_id"].values, "changed_at": at, "from_status": "INFORCE", "to_status": "EXPIRED",
        "created_at": at, "updated_at": at})})
    hist = _concat(acc["contract_status_history"])
    holds = lc.late_status_rows(_status_from_history(hist), con, end)  # V6 edit 14: open holds at the as-of
    holds.insert(0, "status_history_id", ids.take("contract_status_history", len(holds)))
    merge_rows(acc, {"contract_status_history": holds})
    hist = _concat(acc["contract_status_history"])
    last = _status_from_history(hist)
    contract_df = acc["contract"][0]
    contract_df["status"] = contract_df["contract_id"].map(last["to_status"]).to_numpy()
    upd = contract_df["contract_id"].map(last["changed_at"]).to_numpy().astype(str)
    contract_df["updated_at"] = np.where(upd > contract_df["updated_at"].astype(str), upd, contract_df["updated_at"])

    # soft deletes: postings of voided/denied contracts, and a few deleted claim notes
    tables_out = {k: _concat(v) for k, v in acc.items()}
    if len(gone):
        for name in ("transaction", "disbursement", "commission"):
            df = tables_out[name]
            sale_txn = tables_out["transaction"]
            sale_ids = sale_txn.loc[(sale_txn["txn_type"] == "SALE") & sale_txn["contract_id"].isin(gone.index),
                                    "txn_id"]
            m = df["txn_id"].isin(sale_ids)
            df["removed_from_source"] = np.where(m, 1, df.get("removed_from_source", 0))
            df.loc[m, "updated_at"] = df.loc[m, "contract_id"].map(gone).to_numpy()
    late_recordings(tables_out, end, rng)
    notes = tables_out.get("claim_note")
    if notes is not None and len(notes):
        rm = sim.pick(len(notes), 0.005, rng)
        notes["removed_from_source"] = 0
        notes.iloc[rm, notes.columns.get_loc("removed_from_source")] = 1

    # complaints
    comp = []
    c_pick = con.iloc[sim.pick(len(con), 0.006, rng)]
    dn = hdr[hdr["status"] == "DENIED"]
    dn = dn.iloc[sim.pick(len(dn), 0.05, rng)]
    for frame, kind_ in ((c_pick, "contract"), (dn, "claim")):
        if not len(frame):
            continue
        base = (sim.parse_days(frame["entered_date"]) if kind_ == "contract"
                else sim.parse_days(frame["reported_at"]))
        rec = np.minimum(base + rng.integers(20, 400, len(frame)) * DAY, end)
        resolved = rec + rng.integers(3, 45, len(frame)) * DAY
        state = (frame["reportable_state"].to_numpy() if kind_ == "contract"
                 else frame["contract_id"].map(con["reportable_state"]).fillna("TX").to_numpy())
        comp.append(pd.DataFrame({
            "tenant_id": frame["tenant_id"].values,
            "contract_id": frame["contract_id"].values,
            "claim_id": frame["claim_id"].values if kind_ == "claim" else None, "received_date": sim.iso(rec),
            "state": state,
            "category": (sim.weighted_choice(rng, ["CANCELLATION_REFUND", "SERVICE_DELAY", "BILLING"],
                                             [0.5, 0.3, 0.2], len(frame)) if kind_ == "contract"
                         else "CLAIM_DENIAL"),
            "source": sim.weighted_choice(rng, ["CONSUMER", "REGULATOR", "DEALER"], [0.7, 0.15, 0.15], len(frame)),
            "resolution": np.where(resolved <= end, sim.weighted_choice(rng, ["RESOLVED", "NO_ACTION", "REFUNDED"],
                                                                        [0.6, 0.3, 0.1], len(frame)), None),
            "resolved_date": np.where(resolved <= end, sim.iso(resolved), None),
            "created_at": np.char.add(sim.iso(rec), " 11:00:00"),
            "updated_at": np.char.add(sim.iso(np.where(resolved <= end, resolved, rec)), " 11:00:00"),
        }))
    complaints = _concat(comp)
    complaints["is_regulator"] = (complaints["source"] == "REGULATOR").astype(int)
    complaints.insert(0, "complaint_id", np.arange(1, len(complaints) + 1))
    tables_out["complaint"] = complaints

    # custom fields (EAV) for specialty-line contracts: a pure function of the contract rows
    tables_out["custom_field_def"] = cf.field_defs(tables["administrator"]["tenant_id"], created)
    tables_out["contract_custom_field"] = cf.contract_values(tables_out["contract"])

    # ------------------------------------------------------------------ write Admin Core
    core_path.parent.mkdir(parents=True, exist_ok=True)
    core_path.unlink(missing_ok=True)
    counts: dict[str, int] = {}
    with sqlite3.connect(core_path) as conn:
        conn.execute("PRAGMA journal_mode = MEMORY")  # bulk build: one writer, rebuilt from scratch on failure
        conn.execute("PRAGMA synchronous = OFF")
        ac.create_schema(conn)
        conn.executescript(GEN_DDL)
        for name in ac.TABLES:
            df = tables.get(name)
            if df is None:
                df = tables_out.get(name)
            if df is not None and len(df):
                df = df.sort_values("updated_at", kind="stable") if name not in tables else df
                counts[f"admin_core.{name}"] = ac.insert(conn, name, df)
        # generator state: future remittances/voids and every claim plan
        rem, recv = plan["kind"] == "REMIT", plan["received_day"].to_numpy()
        future = plan[(rem & (plan["source"] == "NETWORK") & (recv > end))
                      | (rem & (plan["source"] == "PORTAL") & ((recv > end) | ~booked))
                      | ((plan["kind"] == "VOID") & (plan["void_day"].to_numpy() > end))]
        lc.plan_store(future).to_sql("_gen_feed_plan", conn, if_exists="append", index=False)
        clm.to_store(_concat([core_pl, held_pl])).to_sql("_gen_claim_plan", conn, if_exists="append", index=False)

        # ------------------------------------------------------------------ landing drops
        landing_dir.mkdir(parents=True, exist_ok=True)
        resend: dict[str, list] = {"remittance": [], "econtract_events": []}
        prev = None
        drops = []
        for k in range(landing_days):
            day = cutoff + (k + 1) * DAY
            seq_no = k + 1
            deals = cdeals[cdeals["_sale_day"].to_numpy() == day]
            fp = plan[(plan["kind"] == "REMIT") & (plan["source"] == "NETWORK")
                      & (plan["received_day"].to_numpy() == day)]
            remits = feed_remits(fp)
            voids = plan[(plan["kind"] == "VOID") & (plan["void_day"].to_numpy() == day)]
            voids = voids.assign(day=sim.iso(voids["void_day"].to_numpy()))
            out = write_day(landing_dir, day, seq_no, deals, remits, voids, prev, resend,
                            np.random.default_rng([seed, 7, seq_no]))
            prev = deals
            drops.append(out)
        state = {
            "seed": seed, "as_of": str(as_of), "cutover": str(cat.CUTOVER), "day_index": 0,
            "landing_seq": landing_days, "seq": seq,
            "core_sales_scale": n_core / float(sim.sales_intensity(np.arange(cutover, end + DAY, DAY), cutover).sum()),
            "next_claim_number": int(len(lclaims) + len(cclaims) + 1), "next_held_claim_id": -int(len(held_pl) + 1),
            "next_legacy_cancel": int(len(lcx) + 1), "profiles": profiles, "resend": resend, "intake_seq": 1,
        }
        save_state(conn, state)
        write_epoch(conn, epoch, seed, as_of)

    # ------------------------------------------------------------------ write legacy
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.unlink(missing_ok=True)
    sale_day = lpol.set_index(lcon["legacy_contract_id"].to_numpy())["eff_day"]
    lcl = lg.claim_rows(legacy_claims, cutover, rng)
    ltables = {
        "agents": lg.agents(tables["agent"][tables["agent"]["tenant_id"] == LEGACY_TENANT], created),
        "dealers": lg.dealers(leg_dealers, tables["agent"], created),
        "products": lg.products(ref.rate, ref.plan, ref.comp, created),
        "contracts": lg.mess_contracts(lcon, rng),
        "claims": lg.mess_claims(lcl, lcl["legacy_contract_id"].map(sale_day).to_numpy().astype("M8[D]"), rng),
        "cancellations": lg.mess_cancellations(lg.cancel_rows(lcx, 1), rng),
    }
    with sqlite3.connect(legacy_path) as conn:
        lg.create_schema(conn)
        for name, df in ltables.items():
            df.to_sql(name, conn, if_exists="append", index=False)
            counts[f"legacy.{name}"] = len(df)
        write_epoch(conn, epoch, seed, as_of)
    counts["landing.drops"] = len(drops)
    # CRM / call-center export (FULL history) and the admin-API page drop, next to the landing folder
    intake_dir = itk.default_dir(landing_dir) if intake_dir is None else intake_dir
    for sub in ("crm", "admin_api"):
        if (intake_dir / sub).exists():
            shutil.rmtree(intake_dir / sub)
    counts.update(itk.write_all(core_path, intake_dir, None, end, 1, seed))
    return counts


def feed_remits(fp: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({
        "remit_batch_id": fp["remit_batch_id"].to_numpy(), "dealer_number": fp["dealer_number"].to_numpy(),
        "contract_number": fp["contract_number"].to_numpy(), "remit_amount": fp["amount"].to_numpy(),
        "remit_date": sim.iso(fp["sent_day"].to_numpy()), "entered_date": sim.iso(fp["entered_day"].to_numpy()),
    })


def write_day(landing_dir: Path, day: np.datetime64, seq_no: int, deals: pd.DataFrame, remits: pd.DataFrame,
              voids: pd.DataFrame, prev: pd.DataFrame | None, resend: dict, rng: np.random.Generator) -> Path:
    """Add re-sent rows, inject row defects, write the folder, and queue any file the manifest breaks."""
    from generator.deals import DEAL_COLS

    deals = deals[DEAL_COLS].copy()
    queued = pd.DataFrame(resend.get("remittance", []), columns=remits.columns)
    remits = pd.concat([queued, remits], ignore_index=True) if len(queued) else remits
    prev = prev[DEAL_COLS] if prev is not None else None
    deals, remits, _ = inject_row_defects(deals, remits, rng, seq_no, prev, str(day))
    events = resend.get("econtract_events", []) + contract_events(deals.drop_duplicates("deal_id"), voids, seq_no)
    folder = write_drop(landing_dir, str(day), seq_no, deals, remits, events, f"{day} 05:30:00")
    broken = file_defects(seq_no)
    resend["remittance"] = (remits.astype(object).where(remits.notna(), None).to_dict("records")
                            if "remittance" in broken else [])
    resend["econtract_events"] = events if "econtract_events" in broken else []
    return folder
