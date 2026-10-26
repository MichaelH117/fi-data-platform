"""Synthetic dealer deals (the DMS deal jacket a dealer-integration network forwards daily).

One function makes deals for any period, so the legacy book, the Admin Core history and the
daily landing files all come from the same sales model.
"""

from __future__ import annotations

import zlib

import numpy as np
import pandas as pd

from generator import catalog as cat
from generator import sim
from generator.booking import Ref, rate_deals
from generator.reference import TERM_WEIGHTS

DEAL_COLS = [
    "deal_id", "dealer_number", "contract_number", "vin", "chassis_vin", "sale_date", "product_code", "plan_code",
    "term_months", "term_miles", "odometer", "vehicle_year", "vehicle_make", "vehicle_model", "fuel_type",
    "cylinders", "drive_type", "new_used", "is_commercial", "title_brand", "msrp", "purchase_price",
    "coverage_cost", "retail_price", "surcharge_codes", "option_codes", "finance_type", "finance_amount", "apr",
    "lienholder_code", "customer_first_name", "customer_last_name", "customer_email", "customer_phone",
    "customer_zip", "customer_state",
    # V6 edit 9: the e-contract feed carries a co-buyer; the admin contract never stores one (a known gap).
    "cobuyer_name", "cobuyer_address_line1", "cobuyer_city", "cobuyer_state", "cobuyer_zip",
]
COBUYER_COLS = DEAL_COLS[-5:]
COBUYER_PCT = 25  # share of deals with a co-buyer
STREET_TYPES = ["St", "Ave", "Rd", "Ln", "Dr", "Ct"]
SURCHARGE_SUBCATS = {"VSC-PT", "VSC-SC", "VSC-EN", "VSC-EX", "LTW", "RV", "PSP", "TECH"}


def _pick_dealers(days: np.ndarray, dealers: pd.DataFrame, profiles: dict, rng: np.random.Generator) -> pd.DataFrame:
    numbers = dealers["dealer_number"].to_numpy()
    w = np.array([profiles[c]["weight"] for c in numbers])
    onboard = sim.parse_days(dealers["onboarded_date"])
    eligible = (onboard[None, :] <= days[:, None]) & (dealers["status"].to_numpy() == "ACTIVE")
    probs = eligible * w
    probs /= np.maximum(probs.sum(axis=1, keepdims=True), 1e-12)
    pick = np.minimum((probs.cumsum(axis=1) < rng.random(len(days))[:, None]).sum(axis=1), len(numbers) - 1)
    return dealers.iloc[pick].reset_index(drop=True)


def surcharge_codes(subcat: np.ndarray, v: dict, rng: np.random.Generator) -> np.ndarray:
    """Codes each deal should carry, with deliberate audit errors (missing or spurious codes)."""
    n = len(subcat)
    rules = {
        "DIESEL": v["fuel_type"] == "DIESEL",
        "4X4": v["drive_type"] == "4X4",
        "HYBRID_EV": np.isin(v["fuel_type"], ["HYBRID", "ELECTRIC"]),
        "TURBO": (v["class_code"] == "C") & (v["cylinders"] == 4),
        "COMMERCIAL": v["is_commercial"] == 1,
        "BRANDED_TITLE": pd.notna(v["title_brand"]),
    }
    applies = np.isin(subcat, list(SURCHARGE_SUBCATS))
    out = []
    for i in range(n):
        codes = []
        if applies[i]:
            codes = [c for c, m in rules.items() if m[i] and rng.random() > 0.04]  # 4% missed by the dealer
            if rng.random() < 0.004 and "DIESEL" not in codes and v["fuel_type"][i] == "GAS":
                codes.append("DIESEL")  # charged on a gas vehicle
        out.append("|".join(codes))
    return np.array(out, dtype=object)


def cobuyers(deal_id, last_name, city, state, zips, pools: sim.NamePools) -> dict[str, np.ndarray]:
    """Co-buyer on about a quarter of deals, keyed on deal_id (no random draw moves). Half share the buyer's
    last name; the address is a synthetic street in the dealer's city with the buyer's state and zip."""
    h = np.array([zlib.crc32(f"cobuyer|{d}".encode()) for d in deal_id], dtype=np.int64)
    has = h % 100 < COBUYER_PCT
    first = pools.first[(h // 100) % len(pools.first)]
    last = np.where((h // 7) % 2 == 0, np.asarray(last_name, dtype=object), pools.last[(h // 13) % len(pools.last)])
    street = [f"{100 + int(k) % 9800} {cat.PLACE_WORDS_A[int(k) % len(cat.PLACE_WORDS_A)]} "
              f"{STREET_TYPES[int(k) % len(STREET_TYPES)]}" for k in h // 17]
    def on(v) -> np.ndarray:
        return np.where(has, np.asarray(v, dtype=object), None)
    return {"cobuyer_name": on([f"{a} {b}" for a, b in zip(first, last, strict=True)]),
            "cobuyer_address_line1": on(street), "cobuyer_city": on(city), "cobuyer_state": on(state),
            "cobuyer_zip": on(zips)}


def make_deals(
    days: np.ndarray,
    dealers: pd.DataFrame,
    profiles: dict,
    ref: Ref,
    pools: sim.NamePools,
    rng: np.random.Generator,
    seq: dict[str, int],
    subcats: list[str] | None = None,
) -> pd.DataFrame:
    """One deal per entry of ``days``. ``seq`` holds contract-number counters per prefix plus 'deal'
    and is advanced in place. Returns DEAL_COLS plus helper columns (``_*``)."""
    n = len(days)
    if n == 0:
        return pd.DataFrame(columns=DEAL_COLS)
    d = _pick_dealers(days, dealers, profiles, rng)
    pool = [s for s in cat.SUBCATEGORIES if subcats is None or s.code in subcats]
    sub = sim.weighted_choice(rng, [s.code for s in pool], [s.mix for s in pool], n)
    year = np.clip(days.astype("M8[Y]").astype(int) + 1970, cat.PLAN_YEARS[0], cat.PLAN_YEARS[-1])
    term = np.empty(n, dtype=int)
    ded = np.empty(n, dtype=float)
    for code in np.unique(sub):
        m = sub == code
        s = cat.SUBCAT_BY_CODE[code]
        term[m] = rng.choice(s.terms, m.sum(), p=TERM_WEIGHTS[len(s.terms)])
        ded[m] = rng.choice(s.deductibles, m.sum())
    plan_code = np.array([f"{s}-{y}-D{int(x)}" for s, y, x in zip(sub, year, ded, strict=True)], dtype=object)
    v = sim.vehicles(n, days.astype("M8[Y]").astype(int) + 1970, sub, rng)
    states = d["state"].to_numpy()
    cust_state = np.where(rng.random(n) < 0.95, states, rng.choice(cat.STATES, n)).astype(object)
    sur = surcharge_codes(sub, v, rng)
    opt = np.where(np.char.startswith(sub.astype(str), "VSC") & (rng.random(n) < 0.25),
                   rng.choice(list(cat.OPTIONS), n), "").astype(object)
    finance = sim.weighted_choice(rng, ["CASH", "RETAIL_LOAN", "LEASE", "BALLOON"], [0.15, 0.7, 0.1, 0.05], n)
    fin_amt = np.where(finance == "CASH", None, np.round(v["msrp"] * rng.uniform(0.55, 1.05, n), -2))
    apr = np.where(finance == "CASH", None, np.round(rng.uniform(2.9, 14.9, n), 2))
    lien = np.where(finance == "CASH", None, np.char.add("LH", np.char.zfill(
        rng.integers(1, len(cat.LIENHOLDERS) + 1, n).astype(str), 2))).astype(object)

    prefix = np.array([cat.SUBCAT_BY_CODE[s].prefix for s in sub])
    numbers = np.empty(n, dtype=object)
    for p in np.unique(prefix):
        idx = np.flatnonzero(prefix == p)
        start = seq.get(p, 1)
        numbers[idx] = [f"{p}-{k:08d}" for k in range(start, start + len(idx))]
        seq[p] = start + len(idx)
    start = seq.get("deal", 1)
    deal_id = [f"DL{k:09d}" for k in range(start, start + n)]
    seq["deal"] = start + n

    deals = pd.DataFrame({
        "deal_id": deal_id, "dealer_number": d["dealer_number"].to_numpy(), "contract_number": numbers,
        "vin": v["vin"], "chassis_vin": v["chassis_vin"], "sale_date": sim.iso(days), "product_code": sub,
        "plan_code": plan_code, "term_months": term, "term_miles": None, "odometer": v["odometer"].astype(int),
        "vehicle_year": v["vehicle_year"], "vehicle_make": v["vehicle_make"], "vehicle_model": v["vehicle_model"],
        "fuel_type": v["fuel_type"], "cylinders": v["cylinders"], "drive_type": v["drive_type"],
        "new_used": v["new_used"], "is_commercial": v["is_commercial"], "title_brand": v["title_brand"],
        "msrp": v["msrp"], "purchase_price": None, "coverage_cost": None, "retail_price": None,
        "surcharge_codes": sur, "option_codes": opt, "finance_type": finance, "finance_amount": fin_amt,
        "apr": apr, "lienholder_code": lien,
        **sim.customers(n, cust_state, pools, rng),
    })
    deals = deals.assign(**cobuyers(deals["deal_id"], deals["customer_last_name"], d["city"].to_numpy(),
                                    deals["customer_state"], deals["customer_zip"], pools))
    rated, reasons = rate_deals(deals.drop(columns=["coverage_cost"]), ref)
    if (reasons != "").any():  # the generator must only produce rateable deals
        raise RuntimeError(f"generator produced unrated deals: {reasons[reasons != ''].value_counts().to_dict()}")
    markup = np.array([profiles[c]["markup"] for c in deals["dealer_number"]]) * rng.normal(1.0, 0.06, n)
    cost = rated["_coverage_cost"].to_numpy()
    deals["coverage_cost"] = cost
    deals["purchase_price"] = np.maximum(np.round(cost * np.clip(markup, 1.1, 2.6) / 5) * 5 - 1, cost)
    deals["retail_price"] = (rated["_suggested_retail"] + rated["_sur_amt"] + rated["_opt_amt"]).to_numpy()
    deals["term_miles"] = rated["_term_miles"].to_numpy()
    deals["_sale_day"] = days
    deals["_tenant_id"] = d["tenant_id"].to_numpy()
    deals["_dealer_id"] = d["dealer_id"].to_numpy()
    deals["_subcat"] = sub
    deals["_band"] = rated["_band"].to_numpy()
    deals["_class"] = rated["_class"].to_numpy()
    deals["_ded"] = ded
    deals["_plan_year"] = year
    return deals
