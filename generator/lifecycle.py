"""Contract lifecycle draws shared by the initial build and the daily append: remittance timing,
voids, cancellations and the policy frame the claim simulation runs on."""

from __future__ import annotations

import zlib

import numpy as np
import pandas as pd

from generator import catalog as cat
from generator import sim

DAY = sim.DAY
NEVER_REMIT = 0.012
VOID_RATE = 0.01
# A contract the dealer never remits does not stay PENDING forever: the administrator escalates it
# and auto-voids it this many days after entry (the dealer marts flag the escalation window).
PENDING_AUTO_VOID_DAYS = 120


def batch_id(dealer_number: str, day: str) -> str:
    return f"RB-{dealer_number}-{day.replace('-', '')}"


def _unit(keys) -> np.ndarray:
    """A stable uniform [0, 1) per key (no random draw, so every other draw keeps its place)."""
    return np.array([zlib.crc32(str(k).encode()) for k in keys], dtype=np.int64) / 2**32


def remit_delay(dealer_numbers: np.ndarray, due: np.ndarray, draw: np.ndarray, profiles: dict) -> np.ndarray:
    """Days after the due batch day that a dealer actually enters its remittance.

    Behaviour, not a profile switch: each dealer has its own slip rate (good dealers 3-15% of
    batches, slow dealers 35-80%), a slipped batch is entered 20-44 days late (slow dealers use their
    profile's 15-60 day draw), an on-time batch 0-3 days after the due day, and a batch due over the
    year-end holidays slips another 4-9 days. Keyed on (dealer, due day), so every deal in one batch
    slips together.
    """
    slow = np.array([profiles[d]["remit_delay"][0] >= 10 for d in dealer_numbers])
    u_dealer = _unit(dealer_numbers)
    rate = np.where(slow, 0.35 + 0.45 * u_dealer, 0.03 + 0.12 * u_dealer)
    due_s = sim.iso(due)
    u_batch = _unit([f"{d}|{x}" for d, x in zip(dealer_numbers, due_s, strict=True)])
    slip = u_batch < rate
    late_days = np.where(slow, draw, 20 + (u_batch * 1e6).astype(np.int64) % 25)
    delay = np.where(slip, late_days, np.where(slow, draw % 4, draw))
    md = np.array([x[5:] for x in due_s])
    holiday = (md >= "12-22") | (md <= "01-03")
    return delay + np.where(holiday, 4 + (u_batch * 1e7).astype(np.int64) % 6, 0)


def remit_plan(deals: pd.DataFrame, dealers: pd.DataFrame, profiles: dict, rng: np.random.Generator) -> pd.DataFrame:
    """When each deal is voided or remitted. Returns kind VOID / REMIT / NONE per deal with days."""
    n = len(deals)
    sale = deals["_sale_day"].to_numpy()
    d = dealers.set_index("dealer_number")
    rule = deals["dealer_number"].map(d["remit_rule"]).to_numpy()
    weekday = (sale.astype(int) + 3) % 7  # 0 = Monday
    to_friday = (4 - weekday) % 7
    weekly = sale + np.where(to_friday == 0, 7, to_friday) * DAY
    monthly = (sale.astype("M8[M]") + 1).astype("M8[D]") + 9 * DAY
    batch = np.where(rule == "WEEKLY", weekly, monthly)
    draw = np.array([rng.integers(*profiles[c]["remit_delay"]) if profiles[c]["remit_delay"][1] > 0 else 0
                     for c in deals["dealer_number"]])
    entered = batch + remit_delay(deals["dealer_number"].to_numpy(), batch, draw, profiles) * DAY
    entered_s = sim.iso(entered)
    batches = [batch_id(dn, e) for dn, e in zip(deals["dealer_number"], entered_s, strict=True)]
    h = np.array([zlib.crc32(b.encode()) for b in batches], dtype=np.int64)  # one timeline per batch
    sent = entered + (h % 2) * DAY
    received = sent + (1 + (h // 2) % 3) * DAY
    u = rng.random(n)
    kind = np.where(u < VOID_RATE, "VOID", np.where(u < VOID_RATE + NEVER_REMIT, "NONE", "REMIT"))
    void_day = sale + rng.integers(1, 5, n) * DAY
    portal = np.array([profiles[c]["portal_remitter"] for c in deals["dealer_number"]])
    return pd.DataFrame({
        "kind": kind, "contract_number": deals["contract_number"].to_numpy(),
        "dealer_number": deals["dealer_number"].to_numpy(),
        "remit_batch_id": batches,
        "amount": deals["coverage_cost"].to_numpy(), "entered_day": entered, "sent_day": sent,
        "received_day": received, "void_day": void_day, "source": np.where(portal, "PORTAL", "NETWORK"),
    })


def plan_store(rp: pd.DataFrame) -> pd.DataFrame:
    out = rp.copy()
    for c in ["entered_day", "sent_day", "received_day", "void_day"]:
        out[c] = sim.iso(out[c].to_numpy())
    return out


def plan_load(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in ["entered_day", "sent_day", "received_day", "void_day"]:
        df[c] = sim.parse_days(df[c])
    return df


def cancel_reason(age: np.ndarray, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    names = list(cat.CANCEL_REASONS)
    w = np.array([v[0] for v in cat.CANCEL_REASONS.values()])
    early_w = w * np.array([1.4, 0.4, 0.6, 1.0, 2.5, 2.0])
    late_pick = rng.choice(len(names), len(age), p=w / w.sum())
    early_pick = rng.choice(len(names), len(age), p=early_w / early_w.sum())
    idx = np.where(age < 120, early_pick, late_pick)
    reason = np.array(names, dtype=object)[idx]
    by = np.array([cat.CANCEL_REASONS[r][1] for r in reason], dtype=object)
    return reason, by


def cancel_method(age: np.ndarray, subcat: np.ndarray, by: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    r78 = (subcat == "GAP") | ((by == "LENDER") & (rng.random(len(age)) < 0.2))
    return np.where(age <= cat.FREE_LOOK_DAYS, "FULL_REFUND", np.where(r78, "R78", "PRORATED")).astype(object)


def sample_cancels(p: pd.DataFrame, as_of: np.datetime64, rng: np.random.Generator) -> pd.DataFrame:
    """Lifetime cancellation draw. ``p``: eff_day, active_from, exp_day, subcat, cancel_factor, odometer.
    Returns rows (index aligned to p) for cancels processed on or before ``as_of``."""
    n = len(p)
    tdays = ((p["exp_day"].to_numpy() - p["eff_day"].to_numpy()) / DAY).astype(int)
    early = rng.random(n) < cat.EARLY_CANCEL_SHARE
    offset = np.where(early, rng.exponential(cat.EARLY_CANCEL_MEAN_DAYS, n) + 1, rng.uniform(1, np.maximum(tdays, 2)))
    offset = offset.astype(int)
    day = p["eff_day"].to_numpy() + offset * DAY
    cancellable = p["subcat"].map(lambda s: cat.SUBCAT_BY_CODE[s].cancellable).to_numpy(bool)
    prob = np.clip(cat.LIFETIME_CANCEL_PROB * p["cancel_factor"].to_numpy(), 0, 0.9)
    processed = np.minimum(day + rng.integers(0, 4, n) * DAY, np.maximum(day, as_of))
    hit = (rng.random(n) < prob) & cancellable & (offset < tdays) & (day >= p["active_from"].to_numpy())
    hit &= processed <= as_of
    idx = np.flatnonzero(hit)
    age = offset[idx]
    reason, by = cancel_reason(age, rng)
    sub = p["subcat"].to_numpy()[idx]
    return pd.DataFrame({
        "cancel_day": day[idx], "cancel_date": sim.iso(day[idx]),
        "processed_at": sim.stamp(processed[idx], rng), "initiated_by": by, "reason": reason,
        "method": cancel_method(age, sub, by, rng),
        "odometer": (p["odometer"].to_numpy()[idx] + age * miles_per_day(p)[idx]).astype(int),
        "contract_age_days": age,
    }, index=p.index[idx])


def policies(frame: pd.DataFrame, profiles: dict) -> pd.DataFrame:
    """Normalise a deal-shaped frame into the policy frame used by claims and cancellations."""
    prof = [profiles.get(str(c).strip().upper(), {}) for c in frame["dealer_number"]]
    return pd.DataFrame({
        "subcat": frame["_subcat"].to_numpy(), "term_months": frame["term_months"].astype(int).to_numpy(),
        "eff_day": frame["_sale_day"].to_numpy(),
        "exp_day": sim.add_months(frame["_sale_day"].to_numpy(), frame["term_months"].astype(int).to_numpy()),
        "band": frame["_band"].to_numpy(), "class": frame["_class"].to_numpy(),
        "ded": frame["_ded"].to_numpy(dtype=float), "plan_year": frame["_plan_year"].to_numpy(),
        "new_used": frame["new_used"].to_numpy(), "odometer": frame["odometer"].to_numpy(dtype=float),
        "loss_factor": [x.get("loss_factor", 1.0) for x in prof],
        "cancel_factor": [x.get("cancel_factor", 1.0) for x in prof],
        "wait_days": frame["_subcat"].map(lambda s: cat.SUBCAT_BY_CODE[s].wait_days).to_numpy(),
        "coverage_limit": frame["_subcat"].map(lambda s: cat.SUBCAT_BY_CODE[s].coverage_limit).to_numpy(),
        "dealer_number": frame["dealer_number"].to_numpy(),
    }, index=frame.index)


def with_mileage(p: pd.DataFrame, keys) -> pd.DataFrame:
    """Add each contract's own ``daily_miles`` (drawn once, from its key) and ``mile_end_day``, the day the
    odometer reaches odometer-at-sale + term miles. Coverage ends at the earlier of that and the term."""
    p = p.copy()
    p["daily_miles"] = sim.daily_miles(list(keys), p["subcat"].to_numpy())
    p["mile_end_day"] = sim.mileage_end(p["eff_day"].to_numpy(), p["term_months"].to_numpy(), p["subcat"].to_numpy(),
                                        p["daily_miles"].to_numpy())
    return p


def miles_per_day(p: pd.DataFrame) -> np.ndarray:
    return p["daily_miles"].to_numpy(dtype=float) if "daily_miles" in p else np.full(len(p), 38.0)


# ---------------------------------------------------------------- contract status set (V6 edit 14)
# The 11 contract statuses and every transition the generator writes to contract_status_history.
CONTRACT_STATUSES = ["PENDING", "INFORCE", "CANCELLED", "EXPIRED", "VOIDED", "DENIED", "SUSPENDED",
                     "REINSTATEMENT_PENDING", "REINSTATEMENT_SUSPENDED", "REINSTATEMENT_VOIDED", "REINSTATEMENT_DENIED"]
TRANSITIONS = {
    "PENDING": ["INFORCE", "VOIDED", "DENIED"],           # remitted / voided or never remitted / denied
    "INFORCE": ["CANCELLED", "EXPIRED", "SUSPENDED"],     # SUSPENDED: account hold (e.g. a payment or lien dispute)
    "CANCELLED": ["INFORCE", "REINSTATEMENT_PENDING", "REINSTATEMENT_VOIDED", "REINSTATEMENT_DENIED"],
    "REINSTATEMENT_PENDING": ["REINSTATEMENT_SUSPENDED"],  # request open; suspended while documents are missing
}
SUSPEND_PER_MILLE = 6       # of in-force contracts at the as-of date
REINSTATE_ASK_PCT = 30      # of contracts cancelled in the last REINSTATE_WINDOW_DAYS
REINSTATE_WINDOW_DAYS = 60


def late_status_rows(last: pd.DataFrame, con: pd.DataFrame, as_of: np.datetime64) -> pd.DataFrame:
    """History rows for the three hold statuses, as open items at the as-of date. ``last``: contract_id ->
    to_status, changed_at (the latest history row); ``con``: contract rows indexed by contract_id.
    Hash-keyed on contract_id, so no random draw moves. No money moves on a hold, so nothing else is posted."""
    cols = ["tenant_id", "contract_id", "changed_at", "from_status", "to_status"]
    last = last.reindex(con.index)
    h = np.array([zlib.crc32(f"status|{c}".encode()) for c in con.index], dtype=np.int64)
    changed = np.array([str(x)[:10] if isinstance(x, str) else "1900-01-01" for x in last["changed_at"]], dtype="M8[D]")
    status = last["to_status"].to_numpy()
    end = np.datetime64(as_of, "D")
    exp = sim.parse_days(con["expiration_date"])
    rows = []

    def add(mask, day, frm, to, clock) -> None:
        rows.append(pd.DataFrame({"tenant_id": con["tenant_id"].to_numpy()[mask], "contract_id": con.index[mask],
                                  "changed_at": np.char.add(sim.iso(day[mask]), clock), "from_status": frm,
                                  "to_status": to}))

    sus_day = end - (1 + (h // 1000) % 45) * DAY
    sus = (status == "INFORCE") & (h % 1000 < SUSPEND_PER_MILLE) & (exp > end) & (sus_day > changed)
    add(sus, sus_day, "INFORCE", "SUSPENDED", " 10:15:00")
    ask_day = changed + (2 + (h // 100) % 9) * DAY
    ask = ((status == "CANCELLED") & (h % 100 < REINSTATE_ASK_PCT) & (changed >= end - REINSTATE_WINDOW_DAYS * DAY)
           & (ask_day <= end))
    add(ask, ask_day, "CANCELLED", "REINSTATEMENT_PENDING", " 09:40:00")
    hold_day = ask_day + (3 + (h // 900) % 13) * DAY
    hold = ask & ((h // 300) % 3 == 0) & (hold_day <= end)
    add(hold, hold_day, "REINSTATEMENT_PENDING", "REINSTATEMENT_SUSPENDED", " 14:05:00")
    out = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=cols)
    out["created_at"] = out["updated_at"] = out["changed_at"]
    return out
