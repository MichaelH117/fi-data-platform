"""Vectorised building blocks shared by both source systems and the daily append.

Conventions: dates are numpy ``datetime64[D]``; frames carry helper columns prefixed with
``_`` (hidden simulation state) that are dropped before rows are written to a source.
"""

from __future__ import annotations

import math
import zlib
from dataclasses import dataclass

import numpy as np
import pandas as pd
from faker import Faker

from generator import catalog as cat

DAY = np.timedelta64(1, "D")
NAT = np.datetime64("NaT")


# --------------------------------------------------------------------------- dates
def iso(days: np.ndarray) -> np.ndarray:
    return np.asarray(days).astype("M8[D]").astype(str)


RV_BLANK_CHASSIS_PER_MILLE = 30  # about 3% of RV deals miss their chassis VIN

def stamp(days: np.ndarray, rng: np.random.Generator, lo_hour: int = 8, hi_hour: int = 19) -> np.ndarray:
    """Business-hours timestamp text ('YYYY-MM-DD HH:MM:SS') on each given day."""
    days = np.asarray(days)
    if len(days) == 0:
        return np.array([], dtype="<U19")
    secs = rng.integers(lo_hour * 3600, hi_hour * 3600, size=len(days)).astype("m8[s]")
    out = (days.astype("M8[D]").astype("M8[s]") + secs).astype(str)
    return np.char.replace(out, "T", " ")


def parse_days(values) -> np.ndarray:
    return pd.to_datetime(pd.Series(values), errors="coerce").to_numpy().astype("M8[D]")


def term_days(term_months) -> np.ndarray:
    return np.round(np.asarray(term_months) * 30.4375).astype(int)


def add_months(days: np.ndarray, months) -> np.ndarray:
    m = days.astype("M8[M]") + np.asarray(months).astype("m8[M]")
    dom = (days - days.astype("M8[M]").astype("M8[D]")).astype(int)
    out = m.astype("M8[D]") + np.minimum(dom, 27) * DAY
    return out


def sales_intensity(days: np.ndarray, start: np.datetime64) -> np.ndarray:
    """Relative daily sales volume: ~8%/yr growth, spring peak, quiet weekends."""
    years = (days - start) / np.timedelta64(365, "D")
    doy = (days - days.astype("M8[Y]")).astype(int)
    weekday = (days.astype("M8[D]").astype(int) + 3) % 7  # 0 = Monday
    season = 1 + 0.12 * np.sin(2 * np.pi * (doy - 60) / 365)
    week = np.select([weekday == 6, weekday == 5], [0.55, 0.85], 1.0)
    return (1.08**years) * season * week


def sale_days(start: np.datetime64, end: np.datetime64, n: int, rng: np.random.Generator) -> np.ndarray:
    days = np.arange(start, end + DAY, DAY)
    w = sales_intensity(days, start)
    return np.sort(rng.choice(days, n, p=w / w.sum()))


def beta_pdf(x: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    lgamma = np.vectorize(math.lgamma)
    log_norm = lgamma(a + b) - lgamma(a) - lgamma(b)
    x = np.clip(x, 1e-9, 1 - 1e-9)
    return np.exp(log_norm + (a - 1) * np.log(x) + (b - 1) * np.log1p(-x))


def emergence_shape(new_used: np.ndarray, subcat: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Beta(a, b) of claim emergence over the contract life, by product family.

    VSC (and anything not listed): new cars are back-loaded (factory warranty first), used cars
    roughly even. GAP is front-loaded (the loan balance is furthest above the car's value early on).
    Maintenance is close to uniform (services come at fixed mileage intervals).
    """
    is_new = np.asarray(new_used) == "NEW"
    a, b = np.where(is_new, 2.0, 1.2), np.where(is_new, 1.2, 1.3)
    if subcat is not None:
        subcat = np.asarray(subcat)
        family = np.array([cat.SUBCAT_BY_CODE[s].category if s in cat.SUBCAT_BY_CODE else "" for s in subcat])
        gap, maint = family == "GAP", family == "MAINTENANCE"
        # Ancillary (tire & wheel, key, dent, windshield, appearance, ...) is not held back by a factory
        # warranty, so it emerges about evenly over the term (road hazards and dents slightly early), new or
        # used. The short limited warranty is sold on used cars after any factory cover: also even.
        anc, ltw = family == "ANCILLARY", family == "LIMITED_WARRANTY"
        early = anc & np.isin(subcat, ["TW", "PDR"])
        flat = anc | ltw | maint | gap
        a = np.where(flat, 1.0, a)
        b = np.where(gap, 2.0, np.where(maint, 1.05, np.where(early, 1.1, np.where(flat, 1.0, b))))
    return a, b


# Mileage: every vehicle has its own daily mileage, drawn once per contract (not per claim) and
# reproducible from the contract key, so the build and every appended day agree. Coverage ends at
# whichever limit comes first: the term in months or odometer-at-sale + term miles.
DAILY_MILES = {"RV": (12.0, 0.45), "PSP": (5.0, 0.5)}  # subcategory -> (mean miles/day, lognormal sigma)
DEFAULT_DAILY_MILES = (38.0, 0.3)


def daily_miles(keys, subcat) -> np.ndarray:
    """Lognormal miles per day per contract (mean by subcategory), deterministic in ``keys``."""
    keys = [str(k) for k in keys]
    if not keys:
        return np.array([], dtype=float)
    h1 = np.array([zlib.crc32(f"miles|{k}".encode()) for k in keys], dtype=np.float64)
    h2 = np.array([zlib.crc32(f"miles2|{k}".encode()) for k in keys], dtype=np.float64)
    u1, u2 = (h1 + 0.5) / 2**32, (h2 + 0.5) / 2**32
    z = np.sqrt(-2 * np.log(u1)) * np.cos(2 * np.pi * u2)  # Box-Muller
    par = np.array([DAILY_MILES.get(s, DEFAULT_DAILY_MILES) for s in subcat], dtype=float).reshape(-1, 2)
    mean, sigma = par[:, 0], par[:, 1]
    return np.round(mean * np.exp(sigma * z - sigma**2 / 2), 2)


def mileage_end(eff_day: np.ndarray, term_months, subcat, miles_per_day: np.ndarray) -> np.ndarray:
    """Day the odometer reaches odometer-at-sale + term miles (far future for unlimited-mileage products)."""
    per_month = np.array([cat.SUBCAT_BY_CODE[s].miles_per_month if s in cat.SUBCAT_BY_CODE else 0 for s in subcat])
    term_miles = per_month * np.asarray(term_months, dtype=float)
    days = np.where(per_month > 0, np.floor(term_miles / np.maximum(miles_per_day, 0.1)), 365 * 100)
    return np.asarray(eff_day).astype("M8[D]") + days.astype(int) * DAY


# Report lag (loss -> first notice), shared by the initial build and every appended day so the
# development pattern does not shift as days are appended.
LATE_REPORT_SHARE = 0.05
LATE_REPORT_DAYS = (30, 120)
PROMPT_REPORT_P = 0.3


def report_lag(n: int, rng: np.random.Generator) -> np.ndarray:
    """Days from loss to report: mostly within a week (geometric), a 5% tail of 30-120 days."""
    late = rng.random(n) < LATE_REPORT_SHARE
    return np.where(late, rng.integers(*LATE_REPORT_DAYS, n), rng.geometric(PROMPT_REPORT_P, n) - 1)


# --------------------------------------------------------------------------- vehicles
def make_vins(n: int, rng: np.random.Generator) -> np.ndarray:
    alphabet = np.array(list(cat.VIN_ALPHABET))
    chars = alphabet[rng.integers(0, len(alphabet), size=(n, 17))]
    return chars.view("<U17").ravel() if n else np.array([], dtype="<U17")


def odometer_band(odo: np.ndarray) -> np.ndarray:
    odo = np.asarray(odo)
    out = np.full(len(odo), cat.ODOMETER_BANDS[-1][2], dtype=object)
    for lo, hi, name in cat.ODOMETER_BANDS:
        out[(odo >= lo) & (odo < hi)] = name
    return out


def vehicles(n: int, sale_year: np.ndarray, subcats: np.ndarray, rng: np.random.Generator) -> dict[str, np.ndarray]:
    """Vehicle attributes for ``n`` deals; RV and powersports get their own makes."""
    makes = np.array(list(cat.VEHICLES))
    make = makes[rng.choice(len(makes), n, p=np.array(cat.MAKE_WEIGHTS) / sum(cat.MAKE_WEIGHTS))]
    model = np.array([cat.VEHICLES[m][1][i] for m, i in zip(make, rng.integers(0, 4, n), strict=True)], dtype=object)
    klass = np.array([cat.VEHICLES[m][0] for m in make], dtype=object)
    make = make.astype(object)
    is_rv, is_psp = subcats == "RV", subcats == "PSP"
    coach = list(cat.RV_COACH_MAKES)
    for i in np.flatnonzero(is_rv):
        mk = coach[rng.integers(len(coach))]
        make[i], model[i], klass[i] = mk, cat.RV_COACH_MAKES[mk][rng.integers(2)], "RV"
    psp = list(cat.POWERSPORTS_MAKES)
    for i in np.flatnonzero(is_psp):
        mk = psp[rng.integers(len(psp))]
        make[i], model[i], klass[i] = mk, cat.POWERSPORTS_MAKES[mk][rng.integers(2)], "PS"

    used = rng.random(n) < 0.45
    year = np.where(used, sale_year - rng.integers(1, 8, n), sale_year + (rng.random(n) < 0.3))
    odo = np.where(used, rng.integers(12_000, 98_000, n), rng.integers(3, 150, n))
    is_ev = make == "Tesla"
    fuel = np.where(is_ev, "ELECTRIC", rng.choice(["GAS", "GAS", "GAS", "GAS", "DIESEL", "HYBRID"], n)).astype(object)
    fuel[is_rv] = rng.choice(["GAS", "DIESEL"], is_rv.sum())
    cyl = np.where(is_ev, 0, rng.choice([4, 4, 6, 6, 8], n))
    truck = np.isin(model, ["F-150", "Super Duty", "Silverado", "Tahoe", "Wrangler", "1500", "2500", "3500", "Sierra",
                            "Yukon", "Tacoma", "Frontier", "BT-50"])
    drive = np.where(truck & (rng.random(n) < 0.7), "4X4", np.where(rng.random(n) < 0.35, "AWD", "2WD")).astype(object)
    msrp = np.round(rng.lognormal(np.log(np.where(klass == "C", 62_000, np.where(klass == "RV", 140_000, 38_000))),
                              0.2), -2)
    chassis = np.full(n, None, dtype=object)
    chassis[is_rv] = make_vins(int(is_rv.sum()), rng)
    # A small, fixed share of RV deals arrive with the chassis VIN left blank (a data-quality finding
    # the specialty-line report must surface). Keyed on a hash of the VIN, so no random draw shifts.
    blank = np.array([v is not None and zlib.crc32(str(v).encode()) % 1000 < RV_BLANK_CHASSIS_PER_MILLE
                      for v in chassis], dtype=bool)
    chassis[blank] = None
    return {
        "vin": make_vins(n, rng).astype(object),
        "chassis_vin": chassis,
        "vehicle_year": year.astype(int),
        "vehicle_make": make,
        "vehicle_model": model,
        "class_code": klass,
        "new_used": np.where(used, "USED", "NEW").astype(object),
        "odometer": odo.astype(int),
        "fuel_type": fuel,
        "cylinders": cyl.astype(int),
        "drive_type": drive,
        "is_commercial": (rng.random(n) < 0.03).astype(int),
        "title_brand": np.where(used & (rng.random(n) < 0.02), "SALVAGE_REBUILT", None).astype(object),
        "msrp": msrp,
    }


# --------------------------------------------------------------------------- people
@dataclass
class NamePools:
    first: np.ndarray
    last: np.ndarray

    @classmethod
    def build(cls, seed: int) -> NamePools:
        fake = Faker("en_US")
        fake.seed_instance(seed)
        first = np.array(sorted({fake.first_name() for _ in range(1500)}))
        last = np.array(sorted({fake.last_name() for _ in range(3000)}))
        return cls(first, last)


def customers(n: int, states: np.ndarray, pools: NamePools, rng: np.random.Generator) -> dict[str, np.ndarray]:
    first = pools.first[rng.integers(0, len(pools.first), n)]
    last = pools.last[rng.integers(0, len(pools.last), n)]
    num = rng.integers(1, 999, n)
    email = np.char.lower(np.char.add(np.char.add(np.char.add(first, "."), last), np.char.add(num.astype(str),
                                                                                              "@example.com")))
    phone = np.char.add("555-", np.char.add(np.char.zfill(rng.integers(100, 999, n).astype(str), 3),
                                            np.char.add("-", np.char.zfill(rng.integers(0, 9999, n).astype(str), 4))))
    lo = np.array([cat.ZIP_PREFIX[s][0] for s in states]) if n else np.array([], int)
    hi = np.array([cat.ZIP_PREFIX[s][1] for s in states]) if n else np.array([], int)
    zips = rng.integers(lo * 1000, (hi + 1) * 1000) if n else np.array([], int)
    return {
        "customer_first_name": first.astype(object),
        "customer_last_name": last.astype(object),
        "customer_email": email.astype(object),
        "customer_phone": phone.astype(object),
        "customer_zip": np.char.zfill(zips.astype(str), 5).astype(object),
        "customer_state": np.asarray(states, dtype=object),
    }


# --------------------------------------------------------------------------- misc
def pick(n: int, rate: float, rng: np.random.Generator) -> np.ndarray:
    return np.flatnonzero(rng.random(n) < rate)


def public(df: pd.DataFrame) -> pd.DataFrame:
    return df[[c for c in df.columns if not c.startswith("_")]]


def weighted_choice(rng: np.random.Generator, options: list, weights, n: int) -> np.ndarray:
    w = np.asarray(weights, dtype=float)
    idx = rng.choice(len(options), n, p=w / w.sum())
    return np.array(options, dtype=object)[idx]
