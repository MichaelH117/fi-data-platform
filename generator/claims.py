"""Claim lifecycle simulation for Admin Core (and the legacy claim projection).

A claim is simulated once as a *plan*: report, optional inspection, decision (deny or
authorize), pending payment, one or two payments and close. Most claims settle within about three
weeks, but a realistic tail does not: about 5% wait 30-120 days for parts, an inspection or a
goodwill review before the decision, about 2% sit in PENDING_PAYMENT for 30-90 days waiting on the
invoice, and about 4% of paid claims get a supplemental payment 30-180 days after close.
A claim whose report day is after the as-of date is a true IBNR claim: its plan is stored with a
negative ``claim_id`` and nothing reaches Admin Core until ``--append-day`` reaches the report day.
The plan is stored in ``_gen_claim_plan``; :func:`events` emits the payment lines and history rows that fall inside
a time window. The initial build emits everything up to the as-of date, and each appended
day emits that day's events, so history and incremental days come from the same rules.
"""

from __future__ import annotations

import zlib

import numpy as np
import pandas as pd

from generator import catalog as cat
from generator import sim
from generator.reference import BAND_LOAD, CLASS_LOAD, INFLATION

DAY = sim.DAY
PLAN_COLS = ["claim_id", "claim_number", "tenant_id", "contract_id", "legacy_contract_ref", "subcat", "loss_day",
             "reported_day", "decision_day", "pp_day", "close_day", "inspected", "denied", "two_payments", "gross",
             "ded", "req", "cut", "trim", "auth", "paid_total", "denied_reason", "payee", "adjuster", "hold",
             "supp_day", "supp_amount"]
DATE_COLS = ["loss_day", "reported_day", "decision_day", "pp_day", "close_day", "supp_day"]
DECISION_HOLDS = {"AWAITING_PARTS": 0.020, "AWAITING_INSPECTION": 0.015, "GOODWILL_REVIEW": 0.015}
DECISION_HOLD_DAYS = (30, 121)
PAYMENT_HOLD, PAYMENT_HOLD_SHARE, PAYMENT_HOLD_DAYS = "AWAITING_INVOICE", 0.02, (30, 91)
SUPPLEMENT_SHARE, SUPPLEMENT_DAYS = 0.04, (30, 181)
NOTES = {
    "REPORT": "Claim reported by repair facility; estimate requested.",
    "INSPECT": "Independent inspection completed; estimate adjusted.",
    "APPROVE": "Repair authorized per contract coverage.",
    "DENY": "Claim denied; customer and facility notified.",
    "CONVERT": "Converted from the legacy admin system at cutover.",
    "SUPPLEMENT": "Supplemental invoice approved and paid after close.",
}


def lifetime_lambda(p: pd.DataFrame) -> np.ndarray:
    """Expected lifetime claim count per policy (same drivers as the rate card, plus the dealer factor)."""
    annual = p["subcat"].map(lambda s: cat.SUBCAT_BY_CODE[s].annual_claim_rate).to_numpy()
    band = p["band"].map(BAND_LOAD).fillna(1.0).to_numpy()
    klass = p["class"].map(lambda c: CLASS_LOAD.get(c, (1.0, 1.0))[0]).to_numpy()
    return annual * p["term_months"].to_numpy() / 12 * band * klass * p["loss_factor"].to_numpy()


# Denial reasons for losses outside the waiting period. Pre-existing conditions show up mostly on
# young contracts (the failure started before the sale); WITHIN_WAITING_PERIOD is reserved for
# losses that really fall inside the wait.
IN_WAIT_SHARE = 0.015
OTHER_DENIALS = [r for r in cat.DENIED_REASONS if r != "WITHIN_WAITING_PERIOD"]
DENIAL_WEIGHTS_YOUNG = {"PRE_EXISTING_CONDITION": 0.40, "COMPONENT_NOT_COVERED": 0.20, "MAINTENANCE_NEGLECT": 0.10,
                        "NO_FAILURE_FOUND": 0.15, "MISSING_DOCUMENTATION": 0.15}
DENIAL_WEIGHTS_OLD = {"PRE_EXISTING_CONDITION": 0.08, "COMPONENT_NOT_COVERED": 0.30, "MAINTENANCE_NEGLECT": 0.25,
                      "NO_FAILURE_FOUND": 0.17, "MISSING_DOCUMENTATION": 0.20}
YOUNG_CONTRACT_DAYS = 180


def denial_reasons(age: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """A denial reason per claim (used only where the claim is denied outside the wait)."""
    n = len(age)
    young = sim.weighted_choice(rng, OTHER_DENIALS, [DENIAL_WEIGHTS_YOUNG[r] for r in OTHER_DENIALS], n)
    old = sim.weighted_choice(rng, OTHER_DENIALS, [DENIAL_WEIGHTS_OLD[r] for r in OTHER_DENIALS], n)
    return np.where(np.asarray(age) < YOUNG_CONTRACT_DAYS, young, old).astype(object)


def simulate(p: pd.DataFrame, as_of: np.datetime64, rng: np.random.Generator) -> pd.DataFrame:
    """Draw claims over each policy's life. ``p`` columns: key, tenant_id, contract_id,
    legacy_contract_ref, subcat, term_months, eff_day, active_from, end_day, band, class, ded,
    plan_year, new_used, loss_factor, wait_days, coverage_limit; optional daily_miles and mile_end_day
    (see :func:`generator.lifecycle.with_mileage`): no loss is drawn after the mileage limit is reached."""
    counts = rng.poisson(lifetime_lambda(p))
    par = p.loc[p.index.repeat(counts)].reset_index(drop=True)
    if par.empty:
        return pd.DataFrame(columns=PLAN_COLS)
    a, b = sim.emergence_shape(par["new_used"].to_numpy(), par["subcat"].to_numpy())
    eff = par["eff_day"].to_numpy()
    end = par["end_day"].to_numpy()
    if "mile_end_day" in par:  # coverage also ends when the odometer reaches its term-mile limit
        end = np.minimum(end, par["mile_end_day"].to_numpy().astype("M8[D]"))
    life = (end - eff) / DAY
    full = sim.term_days(par["term_months"].to_numpy())
    wait = par["wait_days"].to_numpy()
    offset = (wait + rng.beta(a, b) * np.maximum(full - wait, 1)).astype(int)
    # A small share of losses happen inside the waiting period (more at high-loss dealers): they are
    # filed after the contract is active and denied WITHIN_WAITING_PERIOD by plan().
    active = np.maximum(((par["active_from"].to_numpy() - eff) / DAY).astype(int), 0)
    share = IN_WAIT_SHARE * par["loss_factor"].to_numpy(dtype=float)
    in_wait = (wait > 0) & (active < wait) & (rng.random(len(par)) < share)
    early = (active + rng.random(len(par)) * np.maximum(wait - active, 1)).astype(int)
    offset = np.where(in_wait, np.minimum(early, np.maximum(wait - 1, 0)), offset)
    loss = eff + offset * DAY
    ok = (offset < life) & (loss >= par["active_from"].to_numpy()) & (loss <= as_of)
    return plan(par[ok].reset_index(drop=True), loss[ok], rng)


def plan(par: pd.DataFrame, loss: np.ndarray, rng: np.random.Generator, report_lag=None) -> pd.DataFrame:
    """Lifecycle plan for one claim per row of ``par`` at the given loss days."""
    n = len(par)
    if n == 0:
        return pd.DataFrame(columns=PLAN_COLS)
    if report_lag is None:
        report_lag = sim.report_lag(n, rng)
    reported = loss + np.asarray(report_lag) * DAY
    sub = par["subcat"].to_numpy()
    sev = np.array([cat.SUBCAT_BY_CODE[s].severity for s in sub])
    s_load = par["class"].map(lambda c: CLASS_LOAD.get(c, (1.0, 1.0))[1]).to_numpy()
    trend = (1 + INFLATION) ** (par["plan_year"].to_numpy() - cat.PLAN_YEARS[0])
    gross = np.round(sev * s_load * trend * rng.lognormal(-0.18, 0.6, n), 2)
    ded = par["ded"].to_numpy(dtype=float)
    req = np.round(np.minimum(np.maximum(gross - ded, 25.0), par["coverage_limit"].to_numpy(dtype=float)), 2)
    age = (loss - par["eff_day"].to_numpy()) / DAY
    wait = par["wait_days"].to_numpy(dtype=float) if "wait_days" in par else np.zeros(n)
    in_wait = age < wait  # a loss inside the waiting period is always denied for that reason
    denied = in_wait | (rng.random(n) < np.where(age < 90, 2 * cat.CLAIM_DENIAL_RATE, cat.CLAIM_DENIAL_RATE))
    u = rng.random(n)
    edges = np.cumsum(list(DECISION_HOLDS.values()))
    hold = np.select([u < e for e in edges], list(DECISION_HOLDS), "").astype(object)
    inspected = (rng.random(n) < np.where(req > 1500, 0.6, 0.25)) | (hold == "AWAITING_INSPECTION")
    cut = np.where(inspected, np.round(req * rng.uniform(0.03, 0.2, n), 2), 0.0)
    trim = np.where(denied, 0.0, np.round((req - cut) * rng.uniform(0, 0.08, n), 2))
    auth = np.where(denied, 0.0, np.round(req - cut - trim, 2))
    partial = ~denied & (rng.random(n) < 0.12)
    paid = np.where(partial, np.round(auth * rng.uniform(0.6, 0.95, n), 2), auth)
    decision = reported + (rng.integers(1, 9, n) + np.where(hold != "", rng.integers(*DECISION_HOLD_DAYS, n), 0)) * DAY
    pay_hold = ~denied & (hold == "") & (rng.random(n) < PAYMENT_HOLD_SHARE)
    hold[pay_hold] = PAYMENT_HOLD
    pp = decision + rng.integers(1, 6, n) * DAY
    close = pp + (rng.integers(2, 12, n) + np.where(pay_hold, rng.integers(*PAYMENT_HOLD_DAYS, n), 0)) * DAY
    supp = ~denied & (rng.random(n) < SUPPLEMENT_SHARE)
    supp_day = np.where(supp, close + rng.integers(*SUPPLEMENT_DAYS, n) * DAY, np.datetime64("NaT"))
    supp_amount = np.where(supp, np.round(np.maximum(paid, 50.0) * rng.uniform(0.05, 0.35, n), 2), 0.0)
    reason = np.where(in_wait, "WITHIN_WAITING_PERIOD", denial_reasons(age, rng))
    miles = par["daily_miles"].to_numpy(dtype=float) if "daily_miles" in par else np.full(n, 38.0)
    return pd.DataFrame({
        "claim_id": par["claim_id"].to_numpy() if "claim_id" in par else np.full(n, -1),
        "claim_number": None,
        "tenant_id": par["tenant_id"].to_numpy(), "contract_id": par["contract_id"].to_numpy(),
        "legacy_contract_ref": par["legacy_contract_ref"].to_numpy(), "subcat": sub,
        "loss_day": loss, "reported_day": reported, "decision_day": decision, "pp_day": pp, "close_day": close,
        "inspected": inspected, "denied": denied, "two_payments": ~denied & (rng.random(n) < 0.25),
        "gross": gross, "ded": ded, "req": req, "cut": cut, "trim": trim, "auth": auth, "paid_total": paid,
        "denied_reason": np.where(denied, reason, None),
        "payee": par["_payee"].to_numpy() if "_payee" in par else None,
        "adjuster": par["_adjuster"].to_numpy() if "_adjuster" in par else None,
        "hold": np.where(hold == "", None, hold), "supp_day": supp_day.astype("M8[D]"), "supp_amount": supp_amount,
        "_odometer": (par["odometer"].to_numpy(dtype=float) + age * miles).astype(int),
        "_towing": (rng.random(n) < 0.08).astype(int), "_goodwill": (rng.random(n) < 0.03).astype(int),
        "_auth_req": (req > 1000).astype(int),
    })


def _ts(day: np.ndarray, claim_id: np.ndarray, kind: str) -> np.ndarray:
    """Deterministic business-hours timestamp per (claim, event kind)."""
    h = np.array([zlib.crc32(f"{c}|{kind}".encode()) % 36_000 for c in claim_id], dtype=np.int64)
    secs = (28_800 + h).astype("m8[s]")
    out = (np.asarray(day).astype("M8[D]").astype("M8[s]") + secs).astype(str)
    return np.char.replace(out, "T", " ")


def status_at(pl: pd.DataFrame, day: np.datetime64) -> tuple[np.ndarray, np.ndarray]:
    """Header (status, substatus) of each planned claim at the end of ``day``."""
    d, pp, close = pl["decision_day"].to_numpy(), pl["pp_day"].to_numpy(), pl["close_day"].to_numpy()
    denied, partial = pl["denied"].to_numpy(bool), (pl["paid_total"] < pl["auth"] - 0.005).to_numpy()
    status = np.select(
        [d > day, denied, pp > day, close > day, partial],
        ["OPEN", "DENIED", "APPROVED", "PENDING_PAYMENT", "CLOSED_PARTIALLY_PAID"],
        "CLOSED_FULLY_PAID",
    )
    hold = pl["hold"].fillna("").to_numpy(str) if "hold" in pl else np.full(len(pl), "")
    open_sub = np.where(np.isin(hold, list(DECISION_HOLDS)), hold,
                        np.where(pl["inspected"].to_numpy(bool), "AWAITING_INSPECTION", "AWAITING_ESTIMATE"))
    sub = np.select(
        [status == "OPEN", status == "APPROVED", status == "PENDING_PAYMENT", status == "DENIED"],
        [open_sub, "AUTHORIZED", np.where(hold == PAYMENT_HOLD, PAYMENT_HOLD, "INVOICE_RECEIVED"), "NOTIFIED"],
        "PAID",
    )
    return status, sub


def events(pl: pd.DataFrame, lo: np.datetime64 | None, hi: np.datetime64) -> dict[str, pd.DataFrame]:
    """Payment lines, history rows and notes for events with day in (lo, hi]."""
    lo = np.datetime64("1900-01-01") if lo is None else lo
    cid, ten = pl["claim_id"].to_numpy(), pl["tenant_id"].to_numpy()
    denied, insp, two = (pl[c].to_numpy(bool) for c in ("denied", "inspected", "two_payments"))
    req, cut, trim, auth, paid, ded, gross = (pl[c].to_numpy(float) for c in
                                              ("req", "cut", "trim", "auth", "paid_total", "ded", "gross"))
    rep, dec, pp, close = (pl[c].to_numpy() for c in ("reported_day", "decision_day", "pp_day", "close_day"))
    partial = ~denied & (paid < auth - 0.005)
    first_pay = np.round(np.where(two, paid * 0.6, paid), 2)
    pay2_day = close
    pay1_day = np.where(two, np.maximum(close - 3 * DAY, pp), close)
    lines, hist, notes = [], [], []

    def add_line(mask, day, kind, auth_c=0.0, insp_c=0.0, paid_c=0.0, pend_c=0.0):
        m = mask & (day > lo) & (day <= hi)
        if m.any():
            sel = lambda v: np.broadcast_to(v, cid.shape)[m]  # noqa: E731
            lines.append(pd.DataFrame({
                "tenant_id": ten[m], "claim_id": cid[m], "posted_at": _ts(day[m], cid[m], kind), "line_type": kind,
                "authorized_change": np.round(sel(auth_c), 2), "inspection_change": np.round(sel(insp_c), 2),
                "paid_amount": np.round(sel(paid_c), 2), "pending_amount": np.round(sel(pend_c), 2),
                "payee_account_holder_id": pl["payee"].to_numpy()[m],
            }))

    def add_hist(mask, day, kind, frm, to, pre=0.0, post=0.0, coll=0.0):
        m = mask & (day > lo) & (day <= hi)
        if m.any():
            sel = lambda v: np.broadcast_to(v, cid.shape)[m]  # noqa: E731
            hist.append(pd.DataFrame({
                "tenant_id": ten[m], "claim_id": cid[m], "changed_at": _ts(day[m], cid[m], kind),
                "from_status": frm if isinstance(frm, str) or frm is None else sel(frm), "to_status": sel(to),
                "pre_deductible_change": np.round(sel(pre), 2), "post_deductible_change": np.round(sel(post), 2),
                "collected_deductible": np.round(sel(coll), 2),
            }))

    def add_note(mask, day, kind):
        m = mask & (day > lo) & (day <= hi)
        if m.any():
            notes.append(pd.DataFrame({"tenant_id": ten[m], "claim_id": cid[m], "author_user_id": pl["adjuster"]
                                       .to_numpy()[m], "noted_at": _ts(day[m], cid[m], "N" + kind),
                                       "note_text": NOTES[kind]}))

    yes = np.ones(len(pl), bool)
    add_line(yes, rep, "REQUEST", pend_c=req)
    add_hist(yes, rep, "H_OPEN", None, "OPEN", pre=gross, post=req)
    add_note(yes, rep, "REPORT")
    add_line(insp, dec, "INSPECTION", insp_c=-cut, pend_c=-cut)
    add_note(insp, dec, "INSPECT")
    add_line(denied, dec, "DENIAL", pend_c=-(req - cut))
    add_hist(denied, dec, "H_DEC", "OPEN", "DENIED", pre=-gross, post=-req)
    add_note(denied, dec, "DENY")
    add_line(~denied, dec, "AUTHORIZATION", auth_c=auth, pend_c=-trim)
    add_hist(~denied, dec, "H_DEC", "OPEN", "APPROVED", pre=auth + ded - gross, post=auth - req)
    add_note(~denied, dec, "APPROVE")
    add_hist(~denied, pp, "H_PP", "APPROVED", "PENDING_PAYMENT")
    add_line(~denied, pay1_day, "PAYMENT", paid_c=first_pay, pend_c=-first_pay)
    add_line(two, pay2_day, "PAYMENT_2", paid_c=paid - first_pay, pend_c=-(paid - first_pay))
    add_line(partial, close, "CLOSE_ADJUST", auth_c=-(auth - paid), pend_c=-(auth - paid))
    final = np.where(partial, "CLOSED_PARTIALLY_PAID", "CLOSED_FULLY_PAID")
    add_hist(~denied, close, "H_CLOSE", "PENDING_PAYMENT", final, coll=ded)
    if "supp_day" in pl:
        sday = pl["supp_day"].to_numpy().astype("M8[D]")
        samt = pl["supp_amount"].fillna(0.0).to_numpy(float)
        has = ~np.isnat(sday) & (samt > 0)
        add_line(has, sday, "SUPPLEMENT", auth_c=samt, paid_c=samt)
        add_note(has, sday, "SUPPLEMENT")
    out = {}
    for name, parts, sort in [("claim_payment_line", lines, "posted_at"), ("claim_history", hist, "changed_at"),
                              ("claim_note", notes, "noted_at")]:
        if parts:
            df = pd.concat(parts, ignore_index=True).sort_values([sort, "claim_id"], kind="stable")
            df["created_at"] = df["updated_at"] = df[sort].to_numpy()
            out[name] = df.reset_index(drop=True)
    return out


def headers(pl: pd.DataFrame, day: np.datetime64, rng: np.random.Generator) -> pd.DataFrame:
    """Claim header rows as of ``day`` (status reflects events up to that day)."""
    status, sub = status_at(pl, day)
    reported = _ts(pl["reported_day"].to_numpy(), pl["claim_id"].to_numpy(), "REQUEST")
    reason = pl["denied_reason"].where(status == "DENIED")
    out = pd.DataFrame({
        "claim_id": pl["claim_id"].to_numpy(), "tenant_id": pl["tenant_id"].to_numpy(),
        "claim_number": pl["claim_number"].to_numpy(), "contract_id": pl["contract_id"].to_numpy(),
        "legacy_contract_ref": pl["legacy_contract_ref"].to_numpy(), "status": status, "substatus": sub,
        "denied_reason": reason.to_numpy(), "denied_category": reason.map(cat.DENIED_REASONS).to_numpy(),
        "repair_facility_id": pl["payee"].to_numpy(), "adjuster_user_id": pl["adjuster"].to_numpy(),
        "loss_date": sim.iso(pl["loss_day"].to_numpy()), "reported_at": reported,
        "odometer": pl["_odometer"].to_numpy() if "_odometer" in pl else None,
        "repair_order": [f"RO-{int(c):07d}" for c in pl["claim_id"]],
        "towing_flag": pl["_towing"].to_numpy() if "_towing" in pl else 0,
        "goodwill_flag": pl["_goodwill"].to_numpy() if "_goodwill" in pl else 0,
        "authorization_required": pl["_auth_req"].to_numpy() if "_auth_req" in pl else 0,
        "requested_amount": pl["req"].to_numpy(), "deductible_amount": pl["ded"].to_numpy(),
        "created_at": reported, "updated_at": reported,
    })
    return out


def last_event_ts(pl: pd.DataFrame, day: np.datetime64) -> np.ndarray:
    """Timestamp of each claim's latest event up to ``day`` (the header's updated_at)."""
    cid = pl["claim_id"].to_numpy()
    cands = [("reported_day", "REQUEST"), ("decision_day", "H_DEC"), ("pp_day", "H_PP"), ("close_day", "H_CLOSE")]
    if "supp_day" in pl:
        cands.append(("supp_day", "SUPPLEMENT"))
    best = _ts(pl["reported_day"].to_numpy(), cid, "REQUEST")
    for col, kind in cands[1:]:
        dd = pl[col].to_numpy().astype("M8[D]")
        valid = ~np.isnat(dd) & (dd <= day)
        if kind != "H_DEC":
            valid &= ~pl["denied"].to_numpy(bool)
        ts = _ts(np.where(np.isnat(dd), pl["reported_day"].to_numpy(), dd), cid, kind)
        best = np.where(valid & (ts > best), ts, best)
    return best


def components(pl: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """1-3 repaired components per claim; parts + labor add up to the gross estimate."""
    rows = []
    for cid, ten, sub, gross, rep in zip(pl["claim_id"], pl["tenant_id"], pl["subcat"], pl["gross"],
                                         _ts(pl["reported_day"].to_numpy(), pl["claim_id"].to_numpy(), "REQUEST"),
                                         strict=True):
        comps = cat.SUBCAT_BY_CODE[sub].components
        k = int(min(len(comps), 1 + rng.poisson(0.4)))
        chosen = rng.choice(len(comps), k, replace=False)
        share = rng.dirichlet(np.ones(k)) if k > 1 else np.array([1.0])
        for c, s in zip(chosen, share, strict=True):
            amt = gross * s
            labor = round(amt * rng.uniform(0.3, 0.5), 2)
            rows.append((ten, cid, comps[c], round(amt - labor, 2), labor, round(labor / 125, 1), rep, rep))
    return pd.DataFrame(rows, columns=["tenant_id", "claim_id", "component_code", "parts_amount", "labor_amount",
                                       "labor_hours", "created_at", "updated_at"])


def to_store(pl: pd.DataFrame) -> pd.DataFrame:
    """Plan -> ``_gen_claim_plan`` rows (dates as ISO text)."""
    out = pl[PLAN_COLS].copy()
    for c in DATE_COLS:
        d = out[c].to_numpy().astype("M8[D]")
        out[c] = np.where(np.isnat(d), None, sim.iso(d))
    for c in ["inspected", "denied", "two_payments"]:
        out[c] = out[c].astype(int)
    return out


def from_store(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in DATE_COLS:
        df[c] = sim.parse_days(df[c]) if c in df else np.full(len(df), np.datetime64("NaT"), "M8[D]")
    if "supp_amount" not in df:
        df["supp_amount"], df["hold"] = 0.0, None
    for c in ["inspected", "denied", "two_payments"]:
        df[c] = df[c].astype(bool)
    return df
