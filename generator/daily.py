"""Append one business day: Admin Core internal work, legacy run-off, and a new landing drop.

Admin Core work happens directly in the OLTP (claims progress and new claims, cancellations,
reinstatements, amendments, expirations, portal remittances, back-office edits, soft deletes).
Network activity (new deals, received remittances, voids) is written as the day's landing folder;
it reaches Admin Core only when ``python -m ingest.feed`` validates and promotes it.
Every write bumps ``row_version`` so the CDC replicator sees exactly the day's changes.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

from generator import admin_core as ac
from generator import catalog as cat
from generator import claims as clm
from generator import intake as itk
from generator import legacy as lg
from generator import lifecycle as lc
from generator import sim
from generator.booking import (
    CLASS_BY_MAKE,
    Ids,
    Ref,
    amend_rows,
    cancel_rows,
    refund_terms,
    reinstate_rows,
    remit_rows,
)
from generator.build import assign_claim_parties, feed_remits, load_state, save_state, write_day
from generator.deals import make_deals
from generator.landing import read_deals

DAY = sim.DAY


def _insert_all(conn: sqlite3.Connection, rows: dict[str, pd.DataFrame]) -> dict[str, int]:
    return {k: ac.insert(conn, k, v) for k, v in rows.items() if v is not None and len(v)}


def _set_status(conn, ids: Ids, contracts: pd.DataFrame, to_status: str, at) -> None:
    if contracts.empty:
        return
    at = np.broadcast_to(np.asarray(at), (len(contracts),))
    ac.update(conn, "contract", contracts["contract_id"].tolist(), {"status": to_status}, list(at))
    hist = pd.DataFrame({
        "status_history_id": ids.take("contract_status_history", len(contracts)),
        "tenant_id": contracts["tenant_id"].values, "contract_id": contracts["contract_id"].values,
        "changed_at": at, "from_status": contracts["status"].values, "to_status": to_status,
        "created_at": at, "updated_at": at})
    ac.insert(conn, "contract_status_history", hist)


def _core_policies(conn: sqlite3.Connection, profiles: dict) -> pd.DataFrame:
    c = pd.read_sql(
        """select c.contract_id, c.tenant_id, c.contract_number, c.status, c.effective_date, c.expiration_date,
                  c.term_months, c.odometer, c.vehicle_make, c.new_used, c.deductible_amount, c.rate_id,
                  c.purchase_price, c.coverage_cost, c.dealer_id, c.agent_id, c.original_obligor_id, c.insurer_id,
                  c.entered_date, s.subcategory_code, p.plan_year, d.dealer_number
           from contract c join plan pl on pl.plan_id = c.plan_id join product p on p.product_id = pl.product_id
           join product_subcategory s on s.subcategory_id = p.subcategory_id join dealer d on d.dealer_id = c.dealer_id
           where c.status = 'INFORCE' and c.removed_from_source = 0""",
        conn,
    )
    f = pd.DataFrame({
        "_subcat": c["subcategory_code"], "term_months": c["term_months"], "_sale_day": sim.parse_days(
            c["effective_date"]), "_band": "ALL", "_class": "ALL", "_ded": c["deductible_amount"],
        "_plan_year": c["plan_year"], "new_used": c["new_used"], "odometer": c["odometer"],
        "dealer_number": c["dealer_number"]})
    rated = f["_subcat"].map(lambda s: cat.SUBCAT_BY_CODE[s].rated_by_vehicle).astype(bool)
    f.loc[rated, "_band"] = sim.odometer_band(f.loc[rated, "odometer"].to_numpy())
    f.loc[rated, "_class"] = c.loc[rated, "vehicle_make"].map(CLASS_BY_MAKE).fillna("B")
    p = lc.policies(f, profiles)
    p["exp_day"] = sim.parse_days(c["expiration_date"])
    p = lc.with_mileage(p, [f"C{int(i)}" for i in c["contract_id"]])
    return p.join(c[["contract_id", "tenant_id", "status"]])


def _legacy_policies(lconn: sqlite3.Connection, profiles: dict) -> pd.DataFrame:
    c = pd.read_sql("select * from contracts where status = 'active' and removed_from_source = 0", lconn)
    c = c.drop_duplicates("legacy_contract_id", keep="last")
    c = c[(c["legacy_contract_id"] < lg.TEST_ID_START) & c["product_code"].isin(cat.SUBCAT_BY_CODE)]
    c["_sale_day"] = sim.parse_days(c["effective_date"])
    c = c[~np.isnat(c["_sale_day"].to_numpy())].reset_index(drop=True)
    rated = c["product_code"].map(lambda s: cat.SUBCAT_BY_CODE[s].rated_by_vehicle).astype(bool)
    f = pd.DataFrame({
        "_subcat": c["product_code"], "term_months": c["term_months"], "_sale_day": c["_sale_day"],
        "_band": np.where(rated, sim.odometer_band(c["odometer_at_sale"].fillna(0).to_numpy()), "ALL"),
        "_class": np.where(rated, c["vehicle_make"].map(CLASS_BY_MAKE).fillna("B"), "ALL"),
        "_ded": c["deductible"], "_plan_year": c["plan_year"], "new_used": c["new_used"],
        "odometer": c["odometer_at_sale"], "dealer_number": c["dealer_code"].fillna("")})
    p = lc.policies(f, profiles)
    p["exp_day"] = sim.parse_days(c["expiration_date"])
    p = lc.with_mileage(p, [f"L{int(i)}" for i in c["legacy_contract_id"]])
    return p.join(c[["legacy_contract_id", "retail_price", "dealer_cost", "admin_fee"]])


def _daily_hazards(p: pd.DataFrame, day: np.datetime64, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    age = ((day - p["eff_day"].to_numpy()) / DAY).astype(float)
    tdays = np.maximum(((p["exp_day"].to_numpy() - p["eff_day"].to_numpy()) / DAY).astype(float), 1)
    cancellable = p["subcat"].map(lambda s: cat.SUBCAT_BY_CODE[s].cancellable).to_numpy(bool)
    early = np.exp(-age / cat.EARLY_CANCEL_MEAN_DAYS) / cat.EARLY_CANCEL_MEAN_DAYS
    prob = np.clip(cat.LIFETIME_CANCEL_PROB * p["cancel_factor"].to_numpy(), 0, 0.9)
    h_cancel = prob * (cat.EARLY_CANCEL_SHARE * early + (1 - cat.EARLY_CANCEL_SHARE) / tdays)
    cancel = (rng.random(len(p)) < h_cancel) & cancellable & (age > 0)
    wait = p["wait_days"].to_numpy()
    span = np.maximum(tdays - wait, 1)
    a, b = sim.emergence_shape(p["new_used"].to_numpy(), p["subcat"].to_numpy())
    u = (age - wait) / span
    lam = clm.lifetime_lambda(p)
    share = np.where(wait > 0, clm.IN_WAIT_SHARE * p["loss_factor"].to_numpy(dtype=float), 0.0)
    h_claim = np.where((u > 0) & (u < 1), (1 - share) * lam * sim.beta_pdf(u, a, b) / span, 0.0)
    # losses inside the waiting period (denied WITHIN_WAITING_PERIOD), the same share as the build
    h_claim = h_claim + np.where((age > 0) & (age < wait), share * lam / np.maximum(wait, 1), 0.0)
    if "mile_end_day" in p:  # no coverage once the odometer passes the term-mile limit
        h_claim = np.where(day > p["mile_end_day"].to_numpy().astype("M8[D]"), 0.0, h_claim)
    claim = (rng.random(len(p)) < h_claim) & ~cancel
    return cancel, claim


def _progress_claims(conn, day, rng) -> int:
    d = str(day)
    pl = pd.read_sql("select * from _gen_claim_plan where claim_id > 0 and "
                     "(decision_day = ? or pp_day = ? or close_day = ? or supp_day = ?)", conn, params=[d, d, d, d])
    if pl.empty:
        return 0
    pl = clm.from_store(pl)
    ids = Ids(conn)
    for name, df in clm.events(pl, day - DAY, day).items():
        df.insert(0, ac.pk(name), ids.take(name, len(df)))
        ac.insert(conn, name, df)
    status, sub = clm.status_at(pl, day)
    reason = pl["denied_reason"].where(status == "DENIED")
    ac.update(conn, "claim", pl["claim_id"].tolist(), {
        "status": list(status), "substatus": list(sub), "denied_reason": list(reason),
        "denied_category": list(reason.map(cat.DENIED_REASONS))}, list(clm.last_event_ts(pl, day)))
    return len(pl)


def _emit_claims(conn, state: dict, pl: pd.DataFrame, day, rng) -> int:
    """Number the plans whose report day has come and write their header, components and events up to
    ``day``; plans reported later are stored with a negative claim_id (IBNR) and wait for their day."""
    due = pl["reported_day"].to_numpy() <= day
    held, pl = pl[~due].copy(), pl[due].copy()
    if len(held):
        nxt = int(state.get("next_held_claim_id", -1))
        held["claim_id"] = np.arange(nxt, nxt - len(held), -1)
        held["claim_number"] = None
        state["next_held_claim_id"] = nxt - len(held)
        clm.to_store(held).to_sql("_gen_claim_plan", conn, if_exists="append", index=False)
    if pl.empty:
        return 0
    pl["claim_id"] = Ids(conn).take("claim", len(pl))
    start = state["next_claim_number"]
    pl["claim_number"] = [str(k) for k in range(start, start + len(pl))]
    state["next_claim_number"] = start + len(pl)
    hdr = clm.headers(pl, day, rng)
    hdr["updated_at"] = clm.last_event_ts(pl, day)
    ac.insert(conn, "claim", hdr)
    comps = clm.components(pl, rng)
    comps.insert(0, "claim_component_id", Ids(conn).take("claim_component", len(comps)))
    ac.insert(conn, "claim_component", comps)
    ids = Ids(conn)
    for name, df in clm.events(pl, None, day).items():
        df.insert(0, ac.pk(name), ids.take(name, len(df)))
        ac.insert(conn, name, df)
    clm.to_store(pl).to_sql("_gen_claim_plan", conn, if_exists="append", index=False)
    return len(pl)


def _release_reported(conn, state: dict, day, rng) -> int:
    """IBNR plans (negative claim_id) whose report day has come become real Admin Core claims today."""
    pl = pd.read_sql("select * from _gen_claim_plan where claim_id < 0 and reported_day <= ?", conn,
                     params=[str(day)])
    if pl.empty:
        return 0
    conn.executemany("delete from _gen_claim_plan where claim_id = ?", [(int(c),) for c in pl["claim_id"]])
    return _emit_claims(conn, state, clm.from_store(pl).sort_values("reported_day", kind="stable"), day, rng)


def _new_claims(conn, lconn, state, core_p, leg_p, core_claim, leg_claim, day, rng, ref_tables) -> int:
    """Losses that occur today. ``clm.plan`` draws their loss-to-report lag from the same model as the
    initial build, so most are reported within days and a few weeks or months later (held as IBNR)."""
    parts = []
    if core_claim.any():
        c = core_p[core_claim].copy()
        c["contract_id"], c["legacy_contract_ref"] = c["contract_id"], None
        parts.append(c)
    if leg_claim.any():
        lp = leg_p[leg_claim].copy()
        lp["tenant_id"], lp["contract_id"] = 1, None
        lp["legacy_contract_ref"] = lp["legacy_contract_id"]
        parts.append(lp)
    if not parts:
        return 0
    par = pd.concat(parts, ignore_index=True)
    par["coverage_limit"] = par["subcat"].map(lambda s: cat.SUBCAT_BY_CODE[s].coverage_limit)
    pl = clm.plan(par, np.full(len(par), day), rng)
    pl = assign_claim_parties(pl, ref_tables, rng)
    return _emit_claims(conn, state, pl, day, rng)


def _portal_remits(conn, plan: pd.DataFrame, day, ids: Ids) -> pd.Index:
    """Process PORTAL remittance events due by ``day``; returns the plan rows that are finished."""
    p = plan[(plan["kind"] == "REMIT") & (plan["source"] == "PORTAL")]
    if p.empty:
        return pd.Index([])
    c = pd.read_sql("select contract_id, contract_number, tenant_id, dealer_id, status from contract", conn)
    c = c.set_index("contract_number")
    p = p[p["contract_number"].isin(c.index)]
    p = p.assign(contract_id=p["contract_number"].map(c["contract_id"]), tenant_id=p["contract_number"].map(
        c["tenant_id"]), dealer_id=p["contract_number"].map(c["dealer_id"]))
    have = set(pd.read_sql("select remit_batch_id from remit_batch", conn)["remit_batch_id"])
    due = p[p["received_day"].to_numpy() <= day]
    pending = c.loc[due["contract_number"], "status"].eq("PENDING").to_numpy()
    due = due[pending]
    if len(due):
        r = due.assign(entered_at=np.char.add(sim.iso(due["entered_day"].to_numpy()), " 09:00:00"),
                       sent_at=np.char.add(sim.iso(due["sent_day"].to_numpy()), " 10:00:00"),
                       received_at=np.char.add(str(day), " 06:20:00"))
        rows = remit_rows(r, ids)
        b = rows.pop("remit_batch")
        old, new = b[b["remit_batch_id"].isin(have)], b[~b["remit_batch_id"].isin(have)]
        ac.insert(conn, "remit_batch", new)
        ac.update(conn, "remit_batch", old["remit_batch_id"].tolist(),
                  {"status": "RECEIVED", "sent_at": list(old["sent_at"]), "received_at": list(old["received_at"])},
                  list(old["received_at"]))
        _insert_all(conn, rows)
        ac.update(conn, "contract", due["contract_id"].tolist(), {"status": "INFORCE"}, f"{day} 06:20:00")
        have |= set(b["remit_batch_id"])
    opening = p[(p["entered_day"].to_numpy() <= day) & (p["received_day"].to_numpy() > day)]
    opening = opening[~opening["remit_batch_id"].isin(have)].drop_duplicates("remit_batch_id")
    if len(opening):
        sent = opening["sent_day"].to_numpy() <= day
        at = f"{day} 09:00:00"
        ac.insert(conn, "remit_batch", pd.DataFrame({
            "remit_batch_id": opening["remit_batch_id"].values, "tenant_id": opening["tenant_id"].values,
            "dealer_id": opening["dealer_id"].values, "entered_at": at,
            "sent_at": np.where(sent, f"{day} 10:00:00", None), "received_at": None,
            "status": np.where(sent, "SENT", "ENTERED"), "source": "PORTAL", "created_at": at, "updated_at": at}))
    return due.index


def _auto_void_pending(conn: sqlite3.Connection, day) -> int:
    """Void contracts still PENDING (no remittance) PENDING_AUTO_VOID_DAYS after entry."""
    cutoff = str(day - lc.PENDING_AUTO_VOID_DAYS * DAY)
    stale = pd.read_sql("select contract_id, tenant_id, status from contract where status = 'PENDING' "
                        "and removed_from_source = 0 and entered_date <= ?", conn, params=[cutoff])
    if stale.empty:
        return 0
    at = f"{day} 16:30:00"
    _set_status(conn, Ids(conn), stale, "VOIDED", at)
    ph = ",".join(map(str, stale["contract_id"].astype(int)))
    txn = [r[0] for r in conn.execute("select txn_id from \"transaction\" where txn_type = 'SALE' "
                                      f"and removed_from_source = 0 and contract_id in ({ph})")]
    if txn:
        tph = ",".join(map(str, txn))
        for table, key in (("transaction", "txn_id"), ("disbursement", "disbursement_id"),
                           ("commission", "commission_id")):
            keys = [r[0] for r in conn.execute(f'select {key} from "{table}" where txn_id in ({tph})')]
            ac.soft_delete(conn, table, keys, at)
    return len(stale)


def append_day(core_path: Path, legacy_path: Path, landing_dir: Path, intake_dir: Path | None = None) -> dict[str, int]:
    """Advance both sources by one day and write that day's landing drop. Deterministic for a seed."""
    with sqlite3.connect(core_path) as conn, sqlite3.connect(legacy_path) as lconn:
        state = load_state(conn)
        state["day_index"] += 1
        day = np.datetime64(state["as_of"]) + DAY
        rng = np.random.default_rng([state["seed"], 100, state["day_index"]])
        profiles = state["profiles"]
        ref = Ref.load(conn)
        ref_tables = {n: ac.read(conn, n) for n in ("account_holder", "admin_user", "dealer")}
        stats: dict[str, int] = {}

        def at(n: int) -> np.ndarray:
            return sim.stamp(np.full(n, day), rng)

        # 1) expirations (Admin Core and legacy)
        exp = pd.read_sql("select contract_id, tenant_id, status, expiration_date from contract "
                          "where status = 'INFORCE' and expiration_date <= ?", conn, params=[str(day)])
        _set_status(conn, Ids(conn), exp, "EXPIRED", f"{day} 00:05:00")
        stats["expired"] = len(exp)
        lconn.execute("update contracts set status = 'expired', updated_at = ? "
                      "where status = 'active' and expiration_date <= ?", [f"{day} 00:05:00", str(day)])

        # 1b) never-remitted contracts: auto-void PENDING_AUTO_VOID_DAYS after entry (sale postings soft-deleted)
        stats["auto_voided"] = _auto_void_pending(conn, day)

        # 2) claims reported today for losses that happened earlier (held back as IBNR), then claims in flight
        stats["claims_progressed"] = _progress_claims(conn, day, rng)  # before the release: no event twice
        stats["claims_released"] = _release_reported(conn, state, day, rng)

        # 3) new claims and cancellations from daily hazards
        core_p = _core_policies(conn, profiles)
        leg_p = _legacy_policies(lconn, profiles)
        c_cancel, c_claim = _daily_hazards(core_p, day, rng)
        l_cancel, l_claim = _daily_hazards(leg_p, day, rng)
        stats["new_claims"] = _new_claims(conn, lconn, state, core_p, leg_p, c_claim, l_claim, day, rng, ref_tables)

        ids = Ids(conn)
        cx_con = core_p[c_cancel]
        if len(cx_con):
            age = ((day - cx_con["eff_day"].to_numpy()) / DAY).astype(int)
            reason, by = lc.cancel_reason(age, rng)
            paid = pd.read_sql("select c.contract_id, sum(l.paid_amount) paid from claim_payment_line l "
                               "join claim c on c.claim_id = l.claim_id group by 1", conn).set_index("contract_id")
            cx = pd.DataFrame({
                "contract_id": cx_con["contract_id"].values, "cancel_date": str(day), "processed_at": at(len(cx_con)),
                "initiated_by": by, "reason": reason,
                "method": lc.cancel_method(age, cx_con["subcat"].to_numpy(), by, rng),
                "odometer": (cx_con["odometer"].to_numpy() + age * lc.miles_per_day(cx_con)).astype(int),
                "withheld": np.where(rng.random(len(cx_con)) < 0.3,
                                     cx_con["contract_id"].map(paid["paid"]).fillna(0.0).to_numpy(), 0.0),
                "from_status": "INFORCE"})
            con = ac.read(conn, "contract", f"contract_id in ({','.join(map(str, cx['contract_id']))})")
            rows = cancel_rows(cx, con, ref, ids)
            _insert_all(conn, {k: v for k, v in rows.items() if k != "contract_status_history"})
            _set_status(conn, ids, con.set_index("contract_id").loc[cx["contract_id"]].reset_index(), "CANCELLED",
                        cx["processed_at"].values)
        stats["core_cancels"] = len(cx_con)
        lx = leg_p[l_cancel]
        if len(lx):
            age = ((day - lx["eff_day"].to_numpy()) / DAY).astype(int)
            reason, by = lc.cancel_reason(age, rng)
            method = lc.cancel_method(age, lx["subcat"].to_numpy(), by, rng)
            terms = refund_terms(pd.DataFrame({
                "purchase_price": lx["retail_price"].values, "coverage_cost": lx["dealer_cost"].values,
                "admin_fee": lx["admin_fee"].values, "term_months": lx["term_months"].values,
                "contract_age_days": age, "method": method, "withheld": 0.0}))
            ts = at(len(lx))
            rows = lg.cancel_rows(pd.DataFrame({
                "legacy_contract_id": lx["legacy_contract_id"].values, "cancel_date": str(day), "reason": reason,
                "method": method, "odometer": (lx["odometer"].to_numpy() + age * lc.miles_per_day(lx)).astype(int),
                "customer_refund": terms["customer_refund"].values, "cancel_fee": terms["cancel_fee"].values,
                "processed_at": ts}), state["next_legacy_cancel"])
            state["next_legacy_cancel"] += len(rows)
            rows.to_sql("cancellations", lconn, if_exists="append", index=False)
            lconn.executemany("update contracts set status = 'cancelled', updated_at = ? where legacy_contract_id = ?",
                              list(zip(ts, lx["legacy_contract_id"].astype(int).tolist(), strict=True)))
        stats["legacy_cancels"] = len(lx)

        # 4) reinstatements of recent cancels
        recent = pd.read_sql(
            "select x.cancel_id, x.contract_id, x.tenant_id, x.coverage_cost_refund from cancellation x "
            "join contract c on c.contract_id = x.contract_id where c.status = 'CANCELLED' "
            "and x.processed_at between ? and ?", conn, params=[f"{day - 40 * DAY}", f"{day - 5 * DAY} 23:59:59"])
        rx = recent.iloc[sim.pick(len(recent), 0.0012, rng)].copy()
        if len(rx):
            rx["reinstated_at"] = at(len(rx))
            rx["status"] = sim.weighted_choice(rng, ["APPROVED", "VOIDED", "DENIED"], [0.75, 0.15, 0.10], len(rx))
            ctx = pd.read_sql("select txn_id, contract_id from \"transaction\" where txn_type = 'CANCEL'", conn)
            ctx = ctx[ctx["contract_id"].isin(rx["contract_id"])]["txn_id"].tolist()
            ph = ",".join(map(str, ctx)) or "NULL"
            disb = ac.read(conn, "disbursement", f"txn_id in ({ph})")
            comm = ac.read(conn, "commission", f"txn_id in ({ph})")
            rows = reinstate_rows(rx, disb, comm, ids)
            rows.pop("contract_status_history")
            _insert_all(conn, rows)
            con = ac.read(conn, "contract", f"contract_id in ({','.join(map(str, rx['contract_id']))})")
            con = con.set_index("contract_id").loc[rx["contract_id"]].reset_index()
            for status, to in (("APPROVED", "INFORCE"), ("VOIDED", "REINSTATEMENT_VOIDED"),
                               ("DENIED", "REINSTATEMENT_DENIED")):
                m = (rx["status"] == status).to_numpy()
                _set_status(conn, ids, con[m], to, rx["reinstated_at"].values[m])
        stats["reinstatements"] = len(rx)

        # 5) amendments, complaints, back-office edits, soft deletes
        am_src = core_p.iloc[sim.pick(len(core_p), 0.0001, rng)]
        if len(am_src):
            am = pd.DataFrame({
                "contract_id": am_src["contract_id"].values, "legacy_contract_ref": None,
                "tenant_id": am_src["tenant_id"].values, "amendment_type": "AMEND", "effective_date": str(day),
                "delta_coverage_cost": np.round(rng.uniform(50, 300, len(am_src)), 2),
                "note": "Coverage change requested by dealer", "at": at(len(am_src))})
            con = ac.read(conn, "contract", f"contract_id in ({','.join(map(str, am['contract_id']))})")
            _insert_all(conn, amend_rows(am, con, ref, ids))
        stats["amendments"] = len(am_src)
        if rng.random() < 0.3 and len(core_p):
            k = core_p.iloc[int(rng.integers(len(core_p)))]
            ts = at(1)
            ac.insert(conn, "complaint", pd.DataFrame([{
                "complaint_id": int(Ids(conn).take("complaint", 1)[0]), "tenant_id": int(k["tenant_id"]),
                "contract_id": int(k["contract_id"]), "claim_id": None, "received_date": str(day),
                "state": None, "category": "SERVICE_DELAY", "source": "CONSUMER", "is_regulator": 0,
                "resolution": None, "resolved_date": None, "created_at": ts[0], "updated_at": ts[0]}]))
        edits = 0
        dealers = ref_tables["dealer"]
        live = dealers[(dealers["status"] == "ACTIVE") & (dealers["is_demo"] == 0)]
        if rng.random() < 0.15:  # SCD2 fodder: a dealer is reassigned to another agent of its tenant
            dd = live.iloc[int(rng.integers(len(live)))]
            agents = ref.agent[(ref.agent["tenant_id"] == dd["tenant_id"]) & (ref.agent["agent_id"] != dd["agent_id"])]
            if len(agents):
                ac.update(conn, "dealer", [int(dd["dealer_id"])],
                          {"agent_id": int(agents["agent_id"].iloc[int(rng.integers(len(agents)))])}, at(1)[0])
                edits += 1
        worst = [c for c, p in profiles.items() if p["loss_factor"] > 1.8]
        worst = live[live["dealer_number"].isin(worst)]
        if len(worst) and rng.random() < 0.005:  # underwriting suspends a bad dealer
            ac.update(conn, "dealer", [int(worst["dealer_id"].iloc[0])], {"status": "SUSPENDED"}, at(1)[0])
            edits += 1
        if len(core_p):  # customer contact corrections
            fix = core_p.iloc[sim.pick(len(core_p), 0.0001, rng)]
            if len(fix):
                ac.update(conn, "contract", fix["contract_id"].tolist(), {"customer_phone": [
                    f"555-{int(x):03d}-{int(y):04d}" for x, y in zip(rng.integers(100, 999, len(fix)),
                                                                     rng.integers(0, 9999, len(fix)), strict=True)]},
                          list(at(len(fix))))
                edits += len(fix)
        if rng.random() < 0.3:  # a claim note entered in error is soft-deleted
            note = conn.execute("select note_id from claim_note where removed_from_source = 0 "
                                "order by note_id desc limit 1 offset ?", [int(rng.integers(0, 200))]).fetchone()
            if note:
                ac.soft_delete(conn, "claim_note", [note[0]], at(1)[0])
                edits += 1
        stats["back_office_edits"] = edits

        # 6) remittances entered through the admin portal (not via the network)
        plan = lc.plan_load(pd.read_sql("select rowid as _rowid, * from _gen_feed_plan", conn)).set_index("_rowid")
        done = _portal_remits(conn, plan, day, ids)

        # 7) today's network drop: new deals, received remittances, voids
        n = int(rng.poisson(state["core_sales_scale"] * sim.sales_intensity(
            np.array([day]), np.datetime64(state["cutover"]))[0]))
        pools = sim.NamePools.build(state["seed"])
        seq = state["seq"]
        deals = make_deals(np.full(n, day), dealers, profiles, ref, pools, rng, seq)
        new_plan = lc.remit_plan(deals, dealers, profiles, rng) if n else pd.DataFrame()
        net = plan[(plan["kind"] == "REMIT") & (plan["source"] == "NETWORK") & (plan["received_day"].to_numpy() <= day)]
        voids = plan[(plan["kind"] == "VOID") & (plan["void_day"].to_numpy() <= day)]
        seq_no = state["landing_seq"] + 1
        prev = read_deals(landing_dir / str(day - DAY))
        write_day(landing_dir, day, seq_no, deals, feed_remits(net),
                  voids.assign(day=sim.iso(voids["void_day"].to_numpy())), prev, state["resend"],
                  np.random.default_rng([state["seed"], 7, seq_no]))
        finished = done.union(net.index).union(voids.index)
        if len(finished):
            conn.executemany("delete from _gen_feed_plan where rowid = ?", [(int(i),) for i in finished])
        if len(new_plan):
            keep = new_plan[new_plan["kind"].isin(["REMIT", "VOID"])]
            lc.plan_store(keep).to_sql("_gen_feed_plan", conn, if_exists="append", index=False)
        stats.update({"new_deals": n, "network_remits": len(net), "voids": len(voids), "portal_remits": len(done)})

        intake_seq = int(state.get("intake_seq", 0)) + 1
        state.update({"as_of": str(day), "landing_seq": seq_no, "seq": seq, "intake_seq": intake_seq})
        save_state(conn, state)
    # 8) CRM delta export and admin-API extract for the day (read the committed Admin Core rows)
    intake_dir = itk.default_dir(landing_dir) if intake_dir is None else intake_dir
    stats.update(itk.write_all(core_path, intake_dir, day - DAY, day, intake_seq, int(state["seed"])))
    return stats
