"""Compute the actuarial tables from contract, claim and payment-line inputs (pure pandas, no I/O).

Conventions
- Whole months: the effective month is month 1 of the term. A cancelled contract earns through its
  cancel month and the rest is refunded, so its UPR drops to 0 that month.
- Cohorts (origin periods) are calendar quarters of the contract effective date ("policy year" style).
  Development age is the quarter a payment line was posted minus the cohort quarter, plus 1.
- Accident quarters are the quarter of the claim loss date; they feed the loss-ratio forecast.
- Valuation is the last fully completed quarter on or before ``as_of``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from actuarial.earning import METHODS, custom_fraction, earned_fraction, mileage_months
from actuarial.methods import assign_methods
from actuarial.reserving import (
    age_to_age_factors,
    bornhuetter_ferguson,
    build_triangle,
    chain_ladder,
    cumulative_factors,
    link_cohort_counts,
)

V1_CURVES = ("pro_rata", "rule_of_78s", "reverse_rule_of_78s")
ALL = "ALL"


@dataclass(frozen=True)
class Settings:
    as_of: date
    tail: float = 1.0
    # Bornhuetter-Ferguson while a cohort has reported less than this share of its ultimate (1 / CDF);
    # chain-ladder once it is more developed than that
    bf_max_reported: float = 0.5
    # a cohort is "mature" (sets the BF a-priori loss ratio) when this share of its term has run, premium
    # weighted (a low CDF is not enough: a young segment's oldest cohort has CDF = tail by construction)
    elr_min_term_elapsed: float = 0.95
    default_elr: float = 0.70  # BF a-priori loss ratio when a segment has no mature or chain-ladder cohort
    default_monthly_miles: float = 1150.0  # used when a segment has no odometer evidence at all
    min_monthly_miles: float = 100.0
    max_monthly_miles: float = 6000.0
    # claims-handling (maintenance) expense on unexpired coverage, as a share of the expected losses;
    # an assumption for the premium-deficiency test, not fitted (no expense data in the warehouse)
    maintenance_expense_ratio: float = 0.05
    # first calendar quarter ("2023Q1") whose development fits the loss factors: only links on
    # diagonals from then on count (reserving.link_cohorts). "auto" = the quarter the newest source
    # system's book starts (the admin-system migration changed how fast losses post); None = all
    # history. A link with fewer than ``factor_min_cohorts`` qualifying cohorts uses all of them.
    factor_start_quarter: str | None = "auto"
    factor_min_cohorts: int = 2


# --------------------------------------------------------------------------- calendar helpers
def _month_index(d: pd.Series) -> pd.Series:
    return d.dt.year * 12 + d.dt.month - 1


def _quarter_index(d: pd.Series) -> pd.Series:
    return d.dt.year * 4 + (d.dt.month - 1) // 3


def _month_start(m: pd.Series) -> pd.Series:
    m = pd.Series(m).astype(int)
    return pd.to_datetime(pd.DataFrame({"year": m // 12, "month": m % 12 + 1, "day": 1}))


def _quarter_start(q: pd.Series) -> pd.Series:
    q = pd.Series(q).astype(int)
    return _month_start(q // 4 * 12 + (q % 4) * 3)


def _quarter_label(q: pd.Series) -> pd.Series:
    q = pd.Series(q).astype(int)
    return (q // 4).astype(str) + "Q" + (q % 4 + 1).astype(str)


def valuation_quarter(as_of: date) -> int:
    """Last fully completed calendar quarter on or before ``as_of`` (as a quarter index)."""
    q = as_of.year * 4 + (as_of.month - 1) // 3
    tomorrow = pd.Timestamp(as_of) + pd.Timedelta(days=1)
    return q if tomorrow.year * 4 + (tomorrow.month - 1) // 3 > q else q - 1


def parse_quarter(label: str) -> int:
    """``"2023Q1"`` -> quarter index."""
    y, q = str(label).upper().split("Q")
    return int(y) * 4 + int(q) - 1


def factor_start(contracts: pd.DataFrame, settings: Settings) -> int | None:
    """Quarter index from which development factors are fitted (see ``Settings.factor_start_quarter``).

    ``auto``: when the book spans more than one source system, the quarter the newest system's
    contracts start (its posting regime is the one every open quarter now develops under).
    """
    sel = settings.factor_start_quarter
    if sel is None:
        return None
    if str(sel).lower() != "auto":
        return parse_quarter(sel)
    if "source_system" not in contracts or contracts["source_system"].nunique() < 2:
        return None
    starts = contracts.dropna(subset=["source_system"]).groupby("source_system")["effective_date"].min()
    return int(_quarter_index(pd.Series([starts.max()])).iloc[0])


def quarter_end(q: int) -> date:
    return (_quarter_start(pd.Series([q + 1])) - pd.Timedelta(days=1)).iloc[0].date()


# --------------------------------------------------------------------------- claim amounts as of
def claim_amounts(claims: pd.DataFrame, lines: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """Per claim: incurred and paid from the payment lines posted on or before ``as_of``."""
    ln = lines[lines["posted_date"] <= pd.Timestamp(as_of)]
    amt = ln.groupby("claim_id")[["incurred", "paid"]].sum()
    out = claims.set_index("claim_id").join(amt, how="left")
    out[["incurred", "paid"]] = out[["incurred", "paid"]].fillna(0.0)
    return out.reset_index()


# --------------------------------------------------------------------------- mileage
def monthly_miles(contracts: pd.DataFrame, claims: pd.DataFrame, settings: Settings) -> pd.Series:
    """Miles driven per month per contract, from odometer readings after the sale.

    Evidence is every claim ``odometer_at_loss`` and the cancellation odometer, each against the sale
    odometer and the months elapsed since the effective date. Contracts with no reading get the
    median of their new/used segment, then ``default_monthly_miles``. Readings are clipped to a
    plausible range so a typo cannot fully earn a contract overnight.
    """
    base = contracts.set_index("contract_id")[["odometer", "effective_date", "new_used"]]
    obs = [
        claims[["contract_id", "odometer_at_loss", "loss_date"]].set_axis(["contract_id", "reading", "at"], axis=1),
        contracts[["contract_id", "cancel_odometer", "cancel_date"]].set_axis(["contract_id", "reading", "at"], axis=1),
    ]
    o = pd.concat(obs, ignore_index=True).dropna()
    o = o.merge(base.rename_axis(None), left_on="contract_id", right_index=True, how="inner")
    months = (o["at"] - o["effective_date"]).dt.days / 30.4375
    o = o.assign(rate=(o["reading"] - o["odometer"]) / months)
    o = o[(months >= 1) & (o["reading"] > o["odometer"])]
    o["rate"] = o["rate"].clip(settings.min_monthly_miles, settings.max_monthly_miles)
    per_contract = o.groupby("contract_id")["rate"].median()
    segment = o.groupby("new_used")["rate"].median()
    rate = base.index.to_series().map(per_contract)
    rate = rate.fillna(base["new_used"].map(segment)).fillna(settings.default_monthly_miles)
    return rate.rename("monthly_miles")


# --------------------------------------------------------------------------- earned premium
CELL = ["category_code", "subcategory_code", "product_code", "new_used", "term_months"]


def _fractions(kk: np.ndarray, rows: np.ndarray, c: pd.DataFrame, curves: dict) -> dict[str, np.ndarray]:
    """Earned fraction after ``kk`` months for each contract row under every method."""
    n = c["term_months"].to_numpy(float)[rows]
    out = {
        "pro_rata": earned_fraction(kk, n, "pro_rata"),
        "rule_of_78s": earned_fraction(kk, n, "rule_of_78s"),
        "reverse_rule_of_78s": earned_fraction(kk, n, "reverse_rule_of_78s"),
        "pro_rata_mileage": np.clip(
            mileage_months(kk, n, c["monthly_miles"].to_numpy(float)[rows], c["term_miles"].to_numpy(float)[rows]) / n,
            0.0, 1.0,
        ),
    }
    method = c["method"].to_numpy()[rows]
    curve_id = c["curve_id"].to_numpy()[rows]
    custom = np.zeros_like(kk)
    for cid, (t, e) in curves.items():
        m = (method == "custom") & (curve_id == cid)
        if m.any():
            custom[m] = custom_fraction(kk[m], n[m], t, e)
    selected = np.select(
        [method == "pro_rata", method == "rule_of_78s", method == "reverse_rule_of_78s",
         method == "pro_rata_mileage", method == "custom"],
        [out["pro_rata"], out["rule_of_78s"], out["reverse_rule_of_78s"], out["pro_rata_mileage"], custom],
        default=out["pro_rata"],
    )
    out["selected"] = selected
    return out


def prepare_contracts(
    contracts: pd.DataFrame, claims: pd.DataFrame, rules: pd.DataFrame, settings: Settings
) -> pd.DataFrame:
    """Contracts effective by ``as_of`` with their earning method and mileage rate attached."""
    c = contracts[contracts["effective_date"] <= pd.Timestamp(settings.as_of)].reset_index(drop=True)
    c = pd.concat([c, assign_methods(c, rules)], axis=1)
    c["monthly_miles"] = c["contract_id"].map(monthly_miles(c, claims, settings)).to_numpy()
    for col in CELL:
        c[col] = c[col].fillna("UNKNOWN") if c[col].dtype == object else c[col]
    return c


def earned_premium(
    c: pd.DataFrame, claim_amt: pd.DataFrame, settings: Settings, curves: dict
) -> pd.DataFrame:
    """Month x product cell x method: written, earned, UPR at month end, in force, incurred, loss ratio."""
    as_of = settings.as_of
    start = _month_index(c["effective_date"]).to_numpy()
    n = c["term_months"].to_numpy(dtype=float)
    cancel_k = (_month_index(c["cancel_date"]) - start + 1).clip(lower=1).to_numpy(dtype=float)  # NaN if none
    asof_k = (as_of.year * 12 + as_of.month - 1) - start + 1
    last_k = np.maximum(np.fmin(np.minimum(n, asof_k), cancel_k), 0).astype(int)

    rows = np.repeat(np.arange(len(c)), last_k)
    k = (np.arange(len(rows)) - np.repeat(np.cumsum(last_k) - last_k, last_k) + 1).astype(float)
    premium = c["premium"].to_numpy(dtype=float)[rows]
    refunded = k == cancel_k[rows]
    month = start[rows] + k.astype(int) - 1

    cell_id, cells = pd.MultiIndex.from_frame(c[CELL]).factorize()
    m0 = int(month.min()) if len(month) else 0
    span = int(month.max()) - m0 + 1 if len(month) else 1
    key = cell_id[rows].astype(np.int64) * span + (month - m0)
    size = len(cells) * span

    def bincount(w: np.ndarray) -> np.ndarray:
        return np.bincount(key, weights=w, minlength=size)

    present = np.bincount(key, minlength=size) > 0
    written = bincount(np.where(k == 1, premium, 0.0))
    n_written = bincount((k == 1).astype(float))
    e_now = _fractions(k, rows, c, curves)
    e_prev = _fractions(k - 1, rows, c, curves)

    # incurred losses by loss month, on the same cells
    cell_of = pd.Series(cell_id, index=c["contract_id"])
    cl = claim_amt[claim_amt["loss_date"] <= pd.Timestamp(as_of)].dropna(subset=["contract_id"])
    cl = cl[cl["contract_id"].isin(cell_of.index)]
    lm = _month_index(cl["loss_date"]).to_numpy()
    ok = (lm >= m0) & (lm < m0 + span)
    ikey = cell_of.loc[cl["contract_id"]].to_numpy()[ok].astype(np.int64) * span + (lm[ok] - m0)
    incurred = np.bincount(ikey, weights=cl["incurred"].to_numpy()[ok], minlength=size)
    present |= np.bincount(ikey, minlength=size) > 0

    idx = np.flatnonzero(present)
    base = pd.DataFrame(np.asarray(cells.to_list(), dtype=object)[idx // span], columns=CELL)
    base["term_months"] = base["term_months"].astype(int)
    month_idx = pd.Series(idx % span + m0)
    base.insert(0, "month", _month_start(month_idx).to_numpy())
    base.insert(0, "posting_period_key", ((month_idx // 12) * 100 + month_idx % 12 + 1).to_numpy())
    frames = []
    for method in METHODS:
        earned = bincount(premium * (e_now[method] - e_prev[method]))
        upr = bincount(np.where(refunded, 0.0, premium * (1 - e_now[method])))
        in_force = bincount((~refunded & (e_now[method] < 1)).astype(float))
        frames.append(base.assign(
            method=method,
            contracts_written=n_written[idx].astype(int),
            written_premium=written[idx],
            earned_premium=earned[idx],
            unearned_premium=upr[idx],
            contracts_in_force=in_force[idx].astype(int),
            incurred_losses=incurred[idx],
        ))
    out = pd.concat(frames, ignore_index=True)
    out["loss_ratio"] = out["incurred_losses"] / out["earned_premium"].where(out["earned_premium"] > 0)
    return out.sort_values(["month", "product_code", "new_used", "method"]).reset_index(drop=True)


def method_assignment(c: pd.DataFrame, rules: pd.DataFrame) -> pd.DataFrame:
    """Which method each category / subcategory / new-used cell got, with the rule's rationale."""
    g = c.groupby(["category_code", "subcategory_code", "new_used", "method", "curve_id"], as_index=False).agg(
        contracts=("contract_id", "size"), premium=("premium", "sum"), avg_monthly_miles=("monthly_miles", "mean")
    )
    why = rules.drop_duplicates(["method", "curve_id"]).set_index(["method", "curve_id"])["rationale"]
    g["rationale"] = [why.get((m, cid), "") for m, cid in zip(g["method"], g["curve_id"], strict=True)]
    return g


# --------------------------------------------------------------------------- does earning mirror emergence?
FIT_CELL = ["category_code", "subcategory_code", "new_used"]
FIT_BANDS = 10  # term deciles
FIT_GAP_WARN = 0.10  # max |curve - emergence| (share of lifetime) above this on a credible row -> warning
FIT_MIN_CLAIMS = 20  # fewer claims than this in a row: shown, but too noisy to gate
FIT_KS_C = 1.36  # one-sample KS critical value at 95%: a gap under 1.36 / sqrt(claims) is noise
FIT_COMPARE = ("pro_rata", "pro_rata_mileage", "rule_of_78s", "reverse_rule_of_78s", "selected")
_DAYS_PER_MONTH = 30.4375


def earning_fit(
    c: pd.DataFrame, claim_amt: pd.DataFrame, settings: Settings, curves: dict,
    gap_warn: float = FIT_GAP_WARN, min_claims: int = FIT_MIN_CLAIMS,
) -> pd.DataFrame:
    """Earned-premium curve vs actual loss emergence, per method cell and per family, at term deciles.

    Base: contracts whose exposure is over by ``as_of`` (term run out, or cancelled), so the lifetime
    is known. A cancelled contract is truncated at its cancel date: it is exposed only up to the
    elapsed share of term it reached, and only its losses dated by then count. Emergence is measured
    life-table style so the truncation does not tilt it early:

    - ``loss_rate[d]`` = incurred in decile ``d`` / premium exposed in ``d`` (a contract cancelled at 35%
      of term is exposed fully in deciles 1-3, half in 4, not at all after);
    - ``emergence_cum_share`` = cumulative ``loss_rate`` / total ``loss_rate`` (share of lifetime
      incurred emerged by the end of the decile, for a contract that runs the full term);
    - ``curve_cum_share`` = premium-weighted share earned by the decile end under the selected method
      (and ``<method>_cum_share`` for the alternatives);
    - ``gap`` = curve - emergence; ``max_abs_gap`` is the KS-style distance per row group;
    - ``ae_ratio`` = (incurred / earned premium in the decile) / (incurred / earned over the life):
      1.0 in every decile when the curve mirrors emergence; > 1 means losses outrun earning there.

    Rows: level ``cell`` (category x subcategory x new/used, one method each) and level ``family``
    (category x new/used, subcategory ``*``; the page charts these). ``noise_band`` = 1.36 / sqrt(claims),
    the 95% Kolmogorov-Smirnov distance a curve that truly fits would still show from sampling alone
    (approximate: losses are dollar-weighted, the count is claims). ``status``:
    ``low_credibility`` under ``min_claims`` claims; ``warn`` when ``max_abs_gap`` is above both
    ``gap_warn`` and ``noise_band``; ``within_noise`` when only above ``gap_warn``; else ``pass``.
    """
    as_of = pd.Timestamp(settings.as_of)
    n = c["term_months"].to_numpy(float)
    term_days = n * _DAYS_PER_MONTH
    term_end = c["effective_date"] + pd.to_timedelta(term_days, unit="D")
    cancelled = c["cancel_date"].notna() & (c["cancel_date"] <= as_of)
    over = (cancelled | (term_end <= as_of)) & (c["premium"] > 0) & (c["term_months"] > 0)
    x = c[over.to_numpy()].reset_index(drop=True)
    cols = [*FIT_CELL, "level", "method", "curve_id", "term_decile", "term_pct", "contracts", "claims",
            "exposed_premium", "earned_premium", "incurred_losses", "emergence_cum_share", "curve_cum_share",
            *[f"{m}_cum_share" for m in FIT_COMPARE if m != "selected"], "gap", "ae_ratio", "max_abs_gap",
            "noise_band", "worst_decile", "best_method", "best_method_gap", "status"]
    if x.empty:
        return pd.DataFrame(columns=cols)

    nx = x["term_months"].to_numpy(float)
    elapsed_days = ((x["cancel_date"] - x["effective_date"]) / pd.Timedelta(days=1)).to_numpy(float)
    elapsed = elapsed_days / (nx * _DAYS_PER_MONTH)
    t_end = np.where(x["cancel_date"].notna() & (x["cancel_date"] <= as_of).to_numpy(), np.clip(elapsed, 0, 1), 1.0)
    prem = x["premium"].to_numpy(float)
    bands = np.arange(FIT_BANDS)
    expo = np.clip(t_end[:, None] * FIT_BANDS - bands[None, :], 0.0, 1.0)  # contract x decile exposure share

    # earned share at each decile edge under every method (full term, no cancellation)
    edges = np.arange(FIT_BANDS + 1) / FIT_BANDS
    rows = np.repeat(np.arange(len(x)), len(edges))
    kk = np.tile(edges, len(x)) * nx[rows]
    fr = {m: v.reshape(len(x), len(edges)) for m, v in _fractions(kk, rows, x, curves).items() if m in FIT_COMPARE}
    sel_inc = np.diff(fr["selected"], axis=1)
    earned = prem[:, None] * sel_inc * expo  # truncated earned premium per decile (pro rata inside a decile)

    # losses by decile of elapsed term, dropped when dated after the exposure ended
    idx = pd.Series(np.arange(len(x)), index=x["contract_id"])
    cl = claim_amt[claim_amt["contract_id"].isin(idx.index) & claim_amt["loss_date"].notna()]
    ci = idx.loc[cl["contract_id"]].to_numpy()
    t_loss = np.clip((cl["loss_date"].to_numpy() - x["effective_date"].to_numpy()[ci]) / pd.Timedelta(days=1)
                     / (nx[ci] * _DAYS_PER_MONTH), 0.0, None)
    keep = t_loss <= t_end[ci] + 1e-9
    ci, t_loss, inc = ci[keep], t_loss[keep], cl["incurred"].to_numpy(float)[keep]
    band = np.minimum((t_loss * FIT_BANDS).astype(int), FIT_BANDS - 1)
    loss = np.zeros((len(x), FIT_BANDS))
    np.add.at(loss, (ci, band), inc)
    n_claims = np.bincount(ci, minlength=len(x))

    groups = []
    for key, g in x.groupby(FIT_CELL, sort=True).groups.items():
        groups.append(("cell", key, np.asarray(g)))
    for (cat, nu), g in x.groupby(["category_code", "new_used"], sort=True).groups.items():
        groups.append(("family", (cat, "*", nu), np.asarray(g)))

    frames = []
    for level, key, r in groups:
        p = prem[r]
        loss_d = loss[r].sum(axis=0)
        exp_d = (p[:, None] * expo[r]).sum(axis=0)
        rate = np.divide(loss_d, exp_d, out=np.zeros(FIT_BANDS), where=exp_d > 0)
        emerg = np.cumsum(rate) / rate.sum() if rate.sum() > 0 else np.full(FIT_BANDS, np.nan)
        cum = {m: (p[:, None] * v[r][:, 1:]).sum(axis=0) / p.sum() for m, v in fr.items()}
        gaps = {m: float(np.nanmax(np.abs(v - emerg))) if rate.sum() > 0 else np.nan for m, v in cum.items()}
        gap = cum["selected"] - emerg
        earned_d = earned[r].sum(axis=0)
        lr_life = loss_d.sum() / earned_d.sum() if earned_d.sum() > 0 else np.nan
        ae = np.divide(loss_d, earned_d, out=np.full(FIT_BANDS, np.nan), where=earned_d > 0) / lr_life
        methods = x.loc[r, "method"].unique()
        curve_ids = sorted({str(v) for v in x.loc[r, "curve_id"].fillna("") if v})
        claims = int(n_claims[r].sum())
        max_gap = gaps["selected"]
        best = min((m for m in gaps if np.isfinite(gaps[m])), key=gaps.get, default="")
        noise = FIT_KS_C / np.sqrt(claims) if claims else np.nan
        status = ("low_credibility" if claims < min_claims or not np.isfinite(max_gap)
                  else "pass" if max_gap <= gap_warn else "warn" if max_gap > noise else "within_noise")
        frame = pd.DataFrame({
            "category_code": key[0], "subcategory_code": key[1], "new_used": key[2], "level": level,
            "method": methods[0] if len(methods) == 1 else "mixed", "curve_id": ",".join(curve_ids),
            "term_decile": bands + 1, "term_pct": edges[1:], "contracts": len(r), "claims": claims,
            "exposed_premium": exp_d, "earned_premium": earned_d, "incurred_losses": loss_d,
            "emergence_cum_share": emerg, "curve_cum_share": cum["selected"],
            **{f"{m}_cum_share": cum[m] for m in FIT_COMPARE if m != "selected"},
            "gap": gap, "ae_ratio": ae, "max_abs_gap": max_gap, "noise_band": noise,
            "worst_decile": int(np.nanargmax(np.abs(gap))) + 1 if np.isfinite(max_gap) else 0,
            "best_method": best, "best_method_gap": gaps.get(best, np.nan), "status": status,
        })
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)[cols]


def earning_fit_summary(fit: pd.DataFrame, gap_warn: float = FIT_GAP_WARN) -> dict[str, object]:
    """run_summary rows: the worst credible gap, how many rows warn, and the gate status."""
    head = fit.drop_duplicates([*FIT_CELL, "level"])
    cred = head[head["status"] != "low_credibility"]
    gated = cred[cred["status"] == "warn"]
    pick = gated if len(gated) else cred  # the worst gap that is more than noise, else the worst credible one
    worst = pick.loc[pick["max_abs_gap"].idxmax()] if len(pick) else None
    label = (f"{worst.category_code}/{worst.subcategory_code}/{worst.new_used} ({worst.method}, decile "
             f"{worst.worst_decile})") if worst is not None else ""
    n_warn = int((head["status"] == "warn").sum())
    return {
        "earning_fit_rows_credible": len(cred),
        "earning_fit_rows_warn": n_warn,
        "earning_fit_worst_gap": float(worst.max_abs_gap) if worst is not None else float("nan"),
        "earning_fit_worst_row": label,
        "earning_fit_gap_threshold": gap_warn,
        "earning_fit_gate": "warn" if n_warn else "pass",
    }


# --------------------------------------------------------------------------- triangles
def _segments(c: pd.DataFrame) -> dict[str, pd.Series]:
    out = {ALL: pd.Series(True, index=c.index)}
    for cat in sorted(c["category_code"].dropna().unique()):
        out[str(cat)] = c["category_code"] == cat
    return out


def policy_triangles(
    c: pd.DataFrame, claims: pd.DataFrame, lines: pd.DataFrame, val_q: int
) -> dict[str, dict[str, pd.DataFrame]]:
    """Per segment: cumulative cohort x age triangles on reported, paid and claim-count bases."""
    c = c.assign(cohort=_quarter_index(c["effective_date"]))
    c = c[c["cohort"] <= val_q]
    cohort_of = c.set_index("contract_id")["cohort"]
    seg_of = c.set_index("contract_id")["category_code"]

    ln = lines[lines["contract_id"].isin(cohort_of.index)]
    ln = ln.assign(cohort=ln["contract_id"].map(cohort_of), q=_quarter_index(ln["posted_date"]))
    ln = ln[ln["q"] <= val_q]
    ln = ln.assign(age=(ln["q"] - ln["cohort"] + 1).clip(lower=1), seg=ln["contract_id"].map(seg_of))

    cl = claims[claims["contract_id"].isin(cohort_of.index)].dropna(subset=["reported_date"])
    cl = cl.assign(cohort=cl["contract_id"].map(cohort_of), q=_quarter_index(cl["reported_date"]))
    cl = cl[cl["q"] <= val_q]
    cl = cl.assign(age=(cl["q"] - cl["cohort"] + 1).clip(lower=1), seg=cl["contract_id"].map(seg_of), n=1.0)

    out = {}
    for seg, mask in _segments(c).items():
        cohorts = sorted(c.loc[mask, "cohort"].unique())
        latest = {int(q): int(val_q - q + 1) for q in cohorts}
        pick_l = ln if seg == ALL else ln[ln["seg"] == seg]
        pick_c = cl if seg == ALL else cl[cl["seg"] == seg]
        out[seg] = {
            "reported": build_triangle(pick_l.assign(value=pick_l["incurred"]), latest),
            "paid": build_triangle(pick_l.assign(value=pick_l["paid"]), latest),
            "count": build_triangle(pick_c.assign(value=pick_c["n"]), latest),
        }
    return out


def triangle_long(triangles: dict[str, dict[str, pd.DataFrame]]) -> pd.DataFrame:
    frames = []
    for seg, bases in triangles.items():
        for basis, tri in bases.items():
            long = tri.stack().rename("cumulative").reset_index()  # stack drops the unobserved NaN cells
            long["incremental"] = long.groupby("cohort")["cumulative"].diff().fillna(long["cumulative"])
            frames.append(long.assign(segment=seg, basis=basis))
    out = pd.concat(frames, ignore_index=True)
    out["cohort_label"] = _quarter_label(out["cohort"])
    out["cohort_start"] = _quarter_start(out["cohort"])
    out = out.rename(columns={"age": "age_quarters"})
    return out[["segment", "basis", "cohort_label", "cohort_start", "age_quarters", "incremental", "cumulative"]]


# --------------------------------------------------------------------------- IBNR
def ultimate_premium(contracts: pd.DataFrame) -> pd.Series:
    """Premium a contract keeps over its life: all of it, or the pro-rata earned share if cancelled.

    This only knows the cancellations that have happened; ``premium_development`` adds the expected
    future ones for young cohorts.
    """
    elapsed = _month_index(contracts["cancel_date"]) - _month_index(contracts["effective_date"]) + 1
    elapsed = elapsed.clip(lower=1).fillna(contracts["term_months"])
    return contracts["premium"] * earned_fraction(elapsed, contracts["term_months"])


def premium_development(c: pd.DataFrame, val_q: int, tail: float = 1.0) -> pd.DataFrame:
    """Per cohort: written, net-of-cancellation premium to date and the expected ultimate net premium.

    A cohort x age triangle of premium kept (written at age 1, less the refunded share in the quarter
    each cancellation lands, counting only cancellations by the valuation quarter) is chain-laddered
    like losses. Its factors are below 1: old cohorts show how much premium a cohort still gives up
    after a given age, so a young cohort is not credited with premium its future cancellations will
    take back. Columns: written_premium, premium_to_date, retention_cdf, ultimate_premium.
    """
    cohort = _quarter_index(c["effective_date"])
    keep = cohort <= val_q
    c, cohort = c[keep], cohort[keep]
    kept = ultimate_premium(c)
    cancel_q = _quarter_index(c["cancel_date"])
    cancelled = cancel_q.notna() & (cancel_q <= val_q)
    kept = kept.where(cancelled, c["premium"])
    latest = {int(q): int(val_q - q + 1) for q in sorted(cohort.unique())}
    if not latest:
        return pd.DataFrame(columns=["written_premium", "premium_to_date", "retention_cdf", "ultimate_premium"])
    rows = pd.concat([
        pd.DataFrame({"cohort": cohort, "age": 1, "value": c["premium"]}),
        pd.DataFrame({"cohort": cohort[cancelled],
                      "age": (cancel_q[cancelled] - cohort[cancelled] + 1).clip(lower=1).astype(int),
                      "value": (kept - c["premium"])[cancelled]}),
    ], ignore_index=True)
    res = chain_ladder(build_triangle(rows, latest), tail=tail)
    out = pd.DataFrame({
        "written_premium": c.groupby(cohort)["premium"].sum(),
        "premium_to_date": res.summary["latest"],
        "retention_cdf": res.summary["cdf"],
        "ultimate_premium": res.summary["ultimate"],
    })
    out.index.name = "cohort"
    return out


def occurred_ibnr(
    c: pd.DataFrame, claims: pd.DataFrame, lines: pd.DataFrame, acc: pd.DataFrame, val_q: int
) -> dict[str, pd.Series]:
    """True IBNR per segment and policy cohort: losses that already happened but are not reported yet.

    Each cohort's reported losses are split by loss quarter and developed with the accident-quarter
    CDF for that loss quarter: ``IBNR = sum_a reported[cohort, a] x (CDF_acc(a) - 1)``. Summed over
    cohorts this equals the accident-quarter IBNR (lines with no contract aside).
    """
    acc_cdf = pd.Series(acc["cdf"].to_numpy(), index=_quarter_index(pd.to_datetime(acc["quarter_start"])).to_numpy())
    loss_q = claims.dropna(subset=["loss_date"]).set_index("claim_id")["loss_date"]
    cc = c.set_index("contract_id")
    ln = lines[lines["contract_id"].isin(cc.index) & lines["claim_id"].isin(loss_q.index)]
    ln = ln[_quarter_index(ln["posted_date"]) <= val_q]
    lq = _quarter_index(ln["claim_id"].map(loss_q))
    ln = ln.assign(
        cohort=_quarter_index(ln["contract_id"].map(cc["effective_date"])),
        seg=ln["contract_id"].map(cc["category_code"]),
        ibnr=ln["incurred"] * (lq.map(acc_cdf).fillna(1.0) - 1.0),
    )
    out = {ALL: ln.groupby("cohort")["ibnr"].sum()}
    for seg, g in ln.groupby("seg"):
        out[str(seg)] = g.groupby("cohort")["ibnr"].sum()
    return out


def ibnr_by_source(claims: pd.DataFrame, lines: pd.DataFrame, acc: pd.DataFrame, val_q: int) -> dict[str, float]:
    """Accident-quarter IBNR split by the system that posted the reported losses it develops.

    ``sum over lines of incurred x (CDF_acc(loss quarter) - 1)``, lines posted and losses dated by
    valuation. Summed over sources it is the accident-quarter IBNR. Lines without a source are "unknown".
    """
    acc_cdf = pd.Series(acc["cdf"].to_numpy(), index=_quarter_index(pd.to_datetime(acc["quarter_start"])).to_numpy())
    loss_q = claims.dropna(subset=["loss_date"]).set_index("claim_id")["loss_date"]
    ln = lines[lines["claim_id"].isin(loss_q.index) & (_quarter_index(lines["posted_date"]) <= val_q)]
    lq = _quarter_index(ln["claim_id"].map(loss_q))
    ln = ln[lq <= val_q]
    ibnr = ln["incurred"] * (lq[lq <= val_q].map(acc_cdf).fillna(1.0) - 1.0)
    src = ln["source_system"].fillna("unknown") if "source_system" in ln else pd.Series("unknown", index=ln.index)
    return {str(k): float(v) for k, v in ibnr.groupby(src).sum().items()}


def _term_mix(c: pd.DataFrame) -> tuple[float, float]:
    """(longest term, premium-weighted mean term) in months."""
    w = c["premium"].clip(lower=0)
    n = c["term_months"].astype(float)
    mean = float((n * w).sum() / w.sum()) if w.sum() > 0 else float(n.mean())
    return float(n.max()), mean


def exposure_tail(c: pd.DataFrame, last_age: int) -> float:
    """1 / share of lifetime exposure run by ``last_age`` quarters (pro rata, premium weighted).

    The oldest cohort is ``last_age`` quarters old, about ``3 x last_age - 1.5`` months on average;
    any contract with a longer term still has exposure left, so the tail is above 1.
    """
    m = 3.0 * last_age - 1.5
    w = c["premium"].clip(lower=0).to_numpy(float)
    share = np.minimum(m / c["term_months"].to_numpy(float), 1.0)
    if not len(share):
        return 1.0
    run = float((share * w).sum() / w.sum()) if w.sum() > 0 else float(share.mean())
    return 1.0 / run if run > 0 else 1.0


def _interp_cdf(cdf: pd.Series, age: float) -> float:
    """CDF at a fractional age (log-linear between ages; flat before age 1; the tail past the last age)."""
    return float(np.exp(np.interp(age, cdf.index.to_numpy(float), np.log(cdf.to_numpy(float)))))


def segment_tails(
    c: pd.DataFrame, triangles: dict[str, dict[str, pd.DataFrame]], settings: Settings,
    factor_start_q: int | None = None,
) -> dict[str, dict[str, object]]:
    """Tail factor per segment and basis: the development a segment still has past its oldest age.

    A segment whose triangle spans its longest term (``3 x ages >= max term months``) has seen its whole
    development: tail = ``Settings.tail`` (source ``observed``). A segment with a shorter history (it
    started recently) borrows one. The reference is the observed segment with the most premium, read at
    the same **share of term** (``ref_age = last_age x mean_term_ref / mean_term_seg``), and an
    exposure-based tail (``exposure_tail``) is the floor; the larger wins (``reference:<SEG>`` or
    ``exposure``). So a segment with terms longer than its history always gets a tail above 1.
    Returns ``{seg: {"reported", "paid", "source", "max_term", "observed_months"}}``.
    """
    fit = {"from_period": factor_start_q, "min_cohorts": settings.factor_min_cohorts}
    info: dict[str, dict] = {}
    for seg in [k for k in triangles if k != ALL]:
        cs = c[c["category_code"] == seg]
        max_term, mean_term = _term_mix(cs) if len(cs) else (0.0, 0.0)
        ages = int(triangles[seg]["reported"].columns.max())
        info[seg] = {"max_term": max_term, "mean_term": mean_term, "ages": ages, "contracts": cs,
                     "observed": 3 * ages >= max_term}
    observed = [k for k, v in info.items() if v["observed"]]
    ref = max(observed, key=lambda k: float(info[k]["contracts"]["premium"].sum())) if observed else None
    ref_cdf = {basis: cumulative_factors(age_to_age_factors(triangles[ref][basis], **fit), settings.tail)
               for basis in ("reported", "paid")} if ref else {}
    out = {}
    for seg, v in info.items():
        row = {"max_term": v["max_term"], "observed_months": 3 * v["ages"]}
        if v["observed"]:
            out[seg] = {**row, "reported": settings.tail, "paid": settings.tail, "source": "observed"}
            continue
        exp_t = max(exposure_tail(v["contracts"], v["ages"]), settings.tail)
        ref_age = v["ages"] * float(info[ref]["mean_term"]) / max(v["mean_term"], 1e-9) if ref else 0.0
        ref_t = {b: _interp_cdf(ref_cdf[b], ref_age) if ref else 1.0 for b in ("reported", "paid")}
        out[seg] = {**row, "reported": max(ref_t["reported"], exp_t), "paid": max(ref_t["paid"], exp_t),
                    "source": f"reference:{ref}" if ref and ref_t["reported"] >= exp_t else "exposure"}
    return out


def term_elapsed_share(c: pd.DataFrame, val_q: int) -> pd.Series:
    """Per cohort: premium-weighted share of term run by the valuation date (a cancelled contract has run)."""
    val_end = pd.Timestamp(quarter_end(val_q))
    months = (val_end - c["effective_date"]).dt.days / _DAYS_PER_MONTH
    share = (months / c["term_months"].astype(float)).clip(0.0, 1.0)
    share = share.where(c["cancel_date"].isna() | (c["cancel_date"] > val_end), 1.0)
    w = c["premium"].clip(lower=0)
    g = pd.DataFrame({"cohort": _quarter_index(c["effective_date"]), "sw": share * w, "w": w, "s": share})
    agg = g.groupby("cohort").agg(sw=("sw", "sum"), w=("w", "sum"), s=("s", "mean"))
    return (agg["sw"] / agg["w"]).where(agg["w"] > 0, agg["s"]).rename("term_elapsed_share")


SUM_COLS = ["contracts", "written_premium", "premium_to_date", "ultimate_premium", "paid_to_date",
            "reported_to_date", "claims_reported", "cl_ultimate", "cl_ibnr", "paid_cl_ultimate", "bf_ultimate",
            "bf_ibnr", "selected_ultimate", "development_to_ultimate", "selected_ibnr", "unexpired_risk_losses"]


def _sum_of_segments(seg_rows: pd.DataFrame, val_q: int) -> pd.DataFrame:
    """The ALL rows: each cohort's segment results added up (ratios recomputed from the sums)."""
    g = seg_rows.assign(_elr_w=seg_rows["bf_expected_loss_ratio"] * seg_rows["ultimate_premium"],
                        _te_w=seg_rows["term_elapsed_share"] * seg_rows["ultimate_premium"])
    t = g.groupby(["cohort_label", "cohort_start"], as_index=False)[[*SUM_COLS, "_elr_w", "_te_w"]].sum()
    q = _quarter_index(pd.to_datetime(t["cohort_start"]))
    prem = t["ultimate_premium"].where(t["ultimate_premium"] > 0)
    t["segment"] = ALL
    t["age_quarters"] = (val_q - q + 1).to_numpy()
    t["premium_retention_cdf"] = t["ultimate_premium"] / t["premium_to_date"].where(t["premium_to_date"] != 0)
    t["cdf"] = t["cl_ultimate"] / t["reported_to_date"].where(t["reported_to_date"] != 0)
    t["tail"] = np.nan
    t["tail_source"] = "sum_of_segments"
    t["term_elapsed_share"] = t["_te_w"] / prem
    t["bf_expected_loss_ratio"] = t["_elr_w"] / prem
    t["elr_source"] = "sum_of_segments"
    t["method"] = "sum_of_segments"
    t["ultimate_loss_ratio"] = t["selected_ultimate"] / prem
    t["valuation_date"] = pd.Timestamp(quarter_end(val_q))
    return t.drop(columns=["_elr_w", "_te_w"])


def ibnr_by_cohort(
    c: pd.DataFrame,
    triangles: dict[str, dict[str, pd.DataFrame]],
    settings: Settings,
    occurred: dict[str, pd.Series] | None = None,
    factor_start_q: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Policy-cohort reserving per segment; the ALL rows are the sum of the segments. Returns (ibnr, factors).

    Premium is the expected **ultimate net** premium (``premium_development``: written, less the
    cancellations so far and those still expected), so a young cohort's BF premium is on the same
    net-of-cancellation basis as the a-priori loss ratio. The ELR comes from **mature** cohorts, those
    that have run at least ``Settings.elr_min_term_elapsed`` of their term (``term_elapsed_share``); a
    segment with none (it is younger than its terms) uses its chain-ladder cohorts, then
    ``Settings.default_elr`` (``elr_source`` says which). A cohort uses BF while it has reported less
    than ``Settings.bf_max_reported`` of its ultimate (1 / CDF), chain-ladder after that.
    ``factor_start_q`` limits the loss factors to diagonals from then on (``link_cohorts``). Past its
    oldest observed age each segment develops with the tail from ``segment_tails`` (``tail``,
    ``tail_source``).

    The cohort triangle develops each sale quarter to its **life-of-contract** ultimate (chain-ladder,
    BF for young cohorts). That ultimate - reported is *not* all IBNR: most of it is losses on coverage
    that has not happened yet (unexpired risk, funded by the unearned premium). So the gap is split:

    - ``selected_ibnr``: true IBNR, losses already incurred but not reported (from ``occurred``,
      which ``occurred_ibnr`` derives from the accident-quarter triangle);
    - ``unexpired_risk_losses``: the rest of the development to ultimate (floored at 0).

    Without ``occurred`` the split is not made and ``selected_ibnr`` is the full development (V1).

    ``ALL`` is the sum of the segment rows, column by column (the standard aggregation: one pooled
    triangle with one ELR would apply old, VSC-heavy factors to young cohorts with a different mix).
    The pooled triangle stays as a diagnostic: ``all_triangle_ultimate`` on the ALL rows, and its
    factors in ``development_factors`` with ``tail_source`` = ``pooled_diagnostic``.
    """
    val_q = valuation_quarter(settings.as_of)
    c = c.assign(cohort=_quarter_index(c["effective_date"]))
    c = c[c["cohort"] <= val_q]
    frames, factor_frames = {}, []
    fit = {"from_period": factor_start_q, "min_cohorts": settings.factor_min_cohorts}
    tails = segment_tails(c, triangles, settings, factor_start_q)
    for seg, mask in _segments(c).items():
        tri = triangles[seg]
        if seg == ALL:
            t_rep = t_paid = settings.tail
            t_src = "pooled_diagnostic"
        else:
            t_rep, t_paid, t_src = tails[seg]["reported"], tails[seg]["paid"], tails[seg]["source"]
        cl = chain_ladder(tri["reported"], tail=t_rep, **fit)
        paid_cl = chain_ladder(tri["paid"], tail=t_paid, **fit)
        prem = premium_development(c[mask], val_q, tail=settings.tail)
        s = cl.summary.join(c[mask].groupby("cohort").agg(contracts=("contract_id", "size"))).join(prem)
        s["premium"] = s["ultimate_premium"]
        s["term_elapsed_share"] = term_elapsed_share(c[mask], val_q).reindex(s.index)
        mature = s["term_elapsed_share"] >= settings.elr_min_term_elapsed
        use_bf = (1.0 / s["cdf"]) < settings.bf_max_reported
        elr, elr_src = settings.default_elr, "default"
        for src, pick in (("mature_cohorts", mature), ("chain_ladder_cohorts", ~use_bf)):
            if pick.any() and s.loc[pick, "premium"].sum() > 0:
                elr, elr_src = s.loc[pick, "ultimate"].sum() / s.loc[pick, "premium"].sum(), src
                break
        bf = bornhuetter_ferguson(s["latest"], s["cdf"], s["premium"], elr)
        cohort = pd.Series(s.index, index=s.index)
        out = pd.DataFrame({
            "segment": seg,
            "cohort_label": _quarter_label(cohort).to_numpy(),
            "cohort_start": _quarter_start(cohort).to_numpy(),
            "age_quarters": s["age"].to_numpy(),
            "contracts": s["contracts"].to_numpy(),
            "written_premium": s["written_premium"].to_numpy(),
            "premium_to_date": s["premium_to_date"].to_numpy(),
            "premium_retention_cdf": s["retention_cdf"].to_numpy(),
            "ultimate_premium": s["premium"].to_numpy(),
            "paid_to_date": tri["paid"].ffill(axis=1).iloc[:, -1].to_numpy(),
            "reported_to_date": s["latest"].to_numpy(),
            "claims_reported": tri["count"].ffill(axis=1).iloc[:, -1].to_numpy(),
            "cdf": s["cdf"].to_numpy(),
            "tail": t_rep,
            "tail_source": t_src,
            "term_elapsed_share": s["term_elapsed_share"].to_numpy(),
            "cl_ultimate": s["ultimate"].to_numpy(),
            "cl_ibnr": s["ibnr"].to_numpy(),
            "paid_cl_ultimate": paid_cl.summary["ultimate"].to_numpy(),
            "bf_expected_loss_ratio": elr,
            "elr_source": elr_src,
            "bf_ultimate": bf["ultimate"].to_numpy(),
            "bf_ibnr": bf["ibnr"].to_numpy(),
            "method": np.where(use_bf, "bornhuetter_ferguson", "chain_ladder"),
        })
        out["selected_ultimate"] = np.where(use_bf.to_numpy(), out["bf_ultimate"], out["cl_ultimate"])
        out["development_to_ultimate"] = out["selected_ultimate"] - out["reported_to_date"]
        if occurred is None:
            out["selected_ibnr"] = out["development_to_ultimate"]
        else:
            occ = occurred.get(seg, pd.Series(dtype=float)).reindex(s.index).fillna(0.0).to_numpy()
            out["selected_ibnr"] = np.minimum(occ, out["development_to_ultimate"].clip(lower=0))
        out["unexpired_risk_losses"] = (out["development_to_ultimate"] - out["selected_ibnr"]).clip(lower=0)
        out["ultimate_loss_ratio"] = out["selected_ultimate"] / out["ultimate_premium"].where(
            out["ultimate_premium"] > 0)
        out["valuation_date"] = pd.Timestamp(quarter_end(val_q))
        frames[seg] = out
        for basis, res, t in (("reported", cl, tri["reported"]), ("paid", paid_cl, tri["paid"])):
            n = link_cohort_counts(t, factor_start_q, settings.factor_min_cohorts)
            factor_frames.append(pd.DataFrame({
                "segment": seg, "basis": basis, "age_quarters": res.cdf.index,
                "ata": res.ata.reindex(res.cdf.index).to_numpy(), "cdf": res.cdf.to_numpy(),
                "cohorts_in_factor": n.reindex(res.cdf.index).fillna(0).astype(int).to_numpy(),
                "tail": t_rep if basis == "reported" else t_paid, "tail_source": t_src,
                "max_term_months": tails[seg]["max_term"] if seg in tails else float(c["term_months"].max()),
                "observed_months": 3 * int(t.columns.max()),
            }))
    pooled = frames.pop(ALL)
    segs = pd.concat(frames.values(), ignore_index=True)
    total = _sum_of_segments(segs, val_q).merge(
        pooled[["cohort_label", "selected_ultimate"]].rename(columns={"selected_ultimate": "all_triangle_ultimate"}),
        on="cohort_label", how="left")
    ibnr = pd.concat([total[[*segs.columns, "all_triangle_ultimate"]], segs], ignore_index=True)
    return ibnr, pd.concat(factor_frames, ignore_index=True)


# --------------------------------------------------------------------------- report-lag IBNR
LAG_MATURE_DAYS = 180  # report lags are measured on losses at least this old, so long lags are not cut off
LAG_HISTORY_DAYS = 730  # ... and at most this much older than that (the current reporting pattern)
CLAIM_RATE_DAYS = 91  # claims per loss day: fitted on the last quarter before valuation
SEVERITY_DAYS = 365  # severity per claim: losses in the year before the claim-rate window


def report_lag_survival(claims: pd.DataFrame, val_end: pd.Timestamp) -> pd.Series:
    """``S(k)`` = share of claims reported more than ``k`` days after the loss (k = 0, 1, ...).

    Fitted on claims with a loss ``LAG_MATURE_DAYS`` to ``LAG_MATURE_DAYS + LAG_HISTORY_DAYS`` before
    valuation that were reported by it. Lags longer than ``LAG_MATURE_DAYS`` are too rare to be cut off.
    """
    cl = claims.dropna(subset=["loss_date", "reported_date"])
    old = val_end - pd.Timedelta(days=LAG_MATURE_DAYS)
    cl = cl[(cl["loss_date"] <= old) & (cl["loss_date"] > old - pd.Timedelta(days=LAG_HISTORY_DAYS))
            & (cl["reported_date"] <= val_end)]
    lag = (cl["reported_date"] - cl["loss_date"]).dt.days.clip(lower=0).to_numpy()
    if not len(lag):
        return pd.Series([0.0], name="survival").rename_axis("lag_days")
    k = np.arange(int(lag.max()) + 1)
    surv = 1.0 - np.searchsorted(np.sort(lag), k, side="right") / len(lag)
    return pd.Series(surv, index=k, name="survival").rename_axis("lag_days")


def report_lag_ibnr(
    claims: pd.DataFrame, lines: pd.DataFrame, val_q: int, factor_start_q: int | None = None,
    min_cohorts: int = 2,
) -> tuple[pd.DataFrame, dict[str, float]]:
    """IBNR per accident quarter from daily report lags, in two parts. Uses only what is known at valuation.

    1. **Unreported claims** (pure IBNR), frequency x severity:
       ``sum over loss days d of  claims_per_day x S(valuation - d) x severity``.
       ``S`` is the report-lag survival (``report_lag_survival``). Claims per day are the reported
       claims of the last ``CLAIM_RATE_DAYS`` loss days grossed up for the ones still to come
       (``n / sum(1 - S)``). Severity is ultimate incurred per claim (denied claims count as 0) on
       losses in the ``SEVERITY_DAYS`` before that window, developed with the factors of part 2.
    2. **Development on claims known at valuation** (case-reserve releases, settlements, supplements):
       a report-quarter triangle (cohort = quarter reported, age = quarters since), chain-laddered with
       links from ``factor_start_q`` on; each line's share goes to its loss quarter.

    The quarterly accident triangle cannot see the first part directly: its age-1 link nets releases on
    known claims against late claims, both summed over a whole quarter of losses, while the late claims
    belong to the last few weeks of losses.

    Returns (per accident quarter index: ``ibnr_unreported``, ``ibnr_known_claims``, ``ibnr``; diagnostics).
    """
    val_end = pd.Timestamp(quarter_end(val_q))
    cl = claims.dropna(subset=["loss_date"])
    cl = cl[cl["loss_date"] <= val_end].assign(reported_date=lambda d: d["reported_date"].fillna(d["loss_date"]))
    known = cl[cl["reported_date"] <= val_end]
    surv = report_lag_survival(known, val_end)

    def s_at(days: np.ndarray) -> np.ndarray:
        return np.where(days < len(surv), surv.to_numpy()[np.minimum(days, len(surv) - 1)], 0.0)

    # 2. development on known claims: report-quarter triangle
    loss_of = cl.set_index("claim_id")["loss_date"]
    rep_of = known.set_index("claim_id")["reported_date"]
    ln = lines[lines["claim_id"].isin(rep_of.index) & (lines["posted_date"] <= val_end)]
    rq = _quarter_index(ln["claim_id"].map(rep_of))
    pq = _quarter_index(ln["posted_date"])
    tri_in = pd.DataFrame({"cohort": rq, "age": (pq - rq + 1).clip(lower=1), "value": ln["incurred"]})
    first = int(rq.min()) if len(rq) else val_q
    tri = build_triangle(tri_in, {q: int(val_q - q + 1) for q in range(first, val_q + 1)})
    rep_cl = chain_ladder(tri, from_period=factor_start_q, min_cohorts=min_cohorts)
    rep_cdf = rep_cl.summary["cdf"]
    dev = ln["incurred"] * (rq.map(rep_cdf).fillna(1.0) - 1.0)
    lq_line = _quarter_index(ln["claim_id"].map(loss_of))
    known_dev = dev.groupby(lq_line).sum()

    # 1. unreported claims: frequency x survival x severity
    age = (val_end - known["loss_date"]).dt.days.to_numpy()
    recent = age < CLAIM_RATE_DAYS
    seen = float((1.0 - s_at(np.arange(CLAIM_RATE_DAYS))).sum())
    per_day = float(recent.sum()) / seen if seen > 0 else 0.0
    sev_win = (age >= CLAIM_RATE_DAYS) & (age < CLAIM_RATE_DAYS + SEVERITY_DAYS)
    sev_ids = known.loc[sev_win, "claim_id"]
    sev_ln = ln[ln["claim_id"].isin(sev_ids)]
    sev_cdf = _quarter_index(sev_ln["claim_id"].map(rep_of)).map(rep_cdf).fillna(1.0)
    sev_ult = float((sev_ln["incurred"] * sev_cdf).sum())
    severity = sev_ult / len(sev_ids) if len(sev_ids) else 0.0
    days = np.arange(len(surv))
    loss_days = val_end - pd.to_timedelta(days, unit="D")
    unrep = pd.Series(per_day * s_at(days) * severity).groupby(_quarter_index(pd.Series(loss_days))).sum()

    out = pd.DataFrame({"ibnr_unreported": unrep, "ibnr_known_claims": known_dev}).fillna(0.0)
    out["ibnr"] = out["ibnr_unreported"] + out["ibnr_known_claims"]
    diag = {
        "ibnr_lag_claims_per_day": per_day,
        "ibnr_lag_mean_days": float(surv.sum()),
        "ibnr_lag_severity": severity,
        "ibnr_unreported_claims_expected": per_day * float(surv.sum()),
        "ibnr_unreported": float(out["ibnr_unreported"].sum()),
        "ibnr_known_claim_development": float(out["ibnr_known_claims"].sum()),
    }
    return out.rename_axis("q"), diag


IBNR_BACKTEST_VALUATIONS = 12


def ibnr_backtest(
    claims: pd.DataFrame, lines: pd.DataFrame, settings: Settings, factor_start_q: int | None = None,
    n: int = IBNR_BACKTEST_VALUATIONS,
) -> pd.DataFrame:
    """The report-lag IBNR re-run at each of the ``n`` quarter-ends before valuation, against what came after.

    Each run sees only claims reported and lines posted by that quarter-end. ``realized`` is the incurred
    posted after it (to ``as_of``) on losses dated by it. One quarter's realized IBNR is a small sum of
    late, skewed claims, so this is information on calibration, not a gate on any single quarter.
    """
    val_q = valuation_quarter(settings.as_of)
    as_of = pd.Timestamp(settings.as_of)
    loss_of = claims.set_index("claim_id")["loss_date"]
    line_loss = lines["claim_id"].map(loss_of)
    rep = claims["reported_date"].fillna(claims["loss_date"])
    rows = []
    for v in range(val_q - n, val_q):
        v_end = pd.Timestamp(quarter_end(v))
        est, _ = report_lag_ibnr(claims[rep <= v_end], lines[lines["posted_date"] <= v_end], v, factor_start_q,
                                 settings.factor_min_cohorts)
        after = (lines["posted_date"] > v_end) & (lines["posted_date"] <= as_of) & (line_loss <= v_end)
        rows.append({"valuation_quarter": str(_quarter_label(pd.Series([v])).iloc[0]), "valuation_date": v_end,
                     "ibnr_estimate": float(est["ibnr"].sum()),
                     "ibnr_unreported": float(est["ibnr_unreported"].sum()),
                     "ibnr_known_claims": float(est["ibnr_known_claims"].sum()),
                     "realized": float(lines.loc[after, "incurred"].sum())})
    out = pd.DataFrame(rows, columns=["valuation_quarter", "valuation_date", "ibnr_estimate", "ibnr_unreported",
                                      "ibnr_known_claims", "realized"])
    out["error"] = out["ibnr_estimate"] - out["realized"]
    return out


def ibnr_backtest_summary(bt: pd.DataFrame) -> dict[str, float]:
    """Mean absolute error, scaled by the mean realized IBNR, and the total estimate / total realized."""
    real = float(bt["realized"].mean()) if len(bt) else float("nan")
    mae = float(bt["error"].abs().mean()) if len(bt) else float("nan")
    return {"ibnr_backtest_valuations": float(len(bt)), "ibnr_backtest_mae": mae,
            "ibnr_backtest_mean_realized": real,
            "ibnr_backtest_scaled_mae": mae / real if real else float("nan"),
            "ibnr_backtest_bias": float(bt["ibnr_estimate"].sum() / bt["realized"].sum())
            if len(bt) and bt["realized"].sum() else float("nan")}


# --------------------------------------------------------------------------- accident quarters
def accident_quarters(
    claims: pd.DataFrame, lines: pd.DataFrame, ep: pd.DataFrame, settings: Settings,
    factor_start_q: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Accident-quarter (loss date) ultimate losses and loss ratios on selected and pro-rata earning.

    IBNR per quarter is the report-lag IBNR (``report_lag_ibnr``); the quarterly chain ladder below
    is kept beside it (``chain_ladder_*``) and for the triangle. Losses develop from the loss quarter to
    the posting quarter. A claim is usually reported within
    weeks of the loss, so this triangle matures fast and is the right base for a loss-ratio forecast.
    The development factors come from diagonals from ``factor_start_q`` on (losses there post under
    the current system; before it the first line posted weeks after the loss, which made the early
    factors too big for the quarters now open). Returns (accident_quarter table, its long
    triangle).
    """
    val_q = valuation_quarter(settings.as_of)
    loss_q = claims.dropna(subset=["loss_date"]).set_index("claim_id")["loss_date"]
    ln = lines[lines["claim_id"].isin(loss_q.index)]
    ln = ln.assign(cohort=_quarter_index(ln["claim_id"].map(loss_q)), q=_quarter_index(ln["posted_date"]))
    ln = ln[(ln["q"] <= val_q) & (ln["cohort"] <= val_q)]
    ln = ln.assign(age=(ln["q"] - ln["cohort"] + 1).clip(lower=1), value=ln["incurred"])
    ep_q = ep.assign(q=_quarter_index(pd.to_datetime(ep["month"])))
    ep_q = ep_q[ep_q["q"] <= val_q].pivot_table(index="q", columns="method", values="earned_premium", aggfunc="sum")
    first = int(min(ln["cohort"].min(), ep_q.index.min())) if len(ln) else int(ep_q.index.min())
    latest = {q: int(val_q - q + 1) for q in range(first, val_q + 1)}
    tri = build_triangle(ln, latest)
    cl = chain_ladder(tri, tail=settings.tail, from_period=factor_start_q, min_cohorts=settings.factor_min_cohorts)
    s = cl.summary
    lag, _ = report_lag_ibnr(claims, lines, val_q, factor_start_q, settings.factor_min_cohorts)
    lag = lag.reindex(s.index).fillna(0.0)
    q = pd.Series(s.index, index=s.index)
    ibnr = lag["ibnr"].to_numpy()
    reported = s["latest"].to_numpy()
    out = pd.DataFrame({
        "accident_quarter": _quarter_label(q).to_numpy(),
        "quarter_start": _quarter_start(q).to_numpy(),
        "age_quarters": s["age"].to_numpy(),
        "earned_premium": ep_q["selected"].reindex(s.index).fillna(0).to_numpy(),
        "earned_premium_pro_rata": ep_q["pro_rata"].reindex(s.index).fillna(0).to_numpy(),
        "reported": reported,
        # the report-lag IBNR, expressed as a factor on reported so lines can carry it to cohorts and sources
        "cdf": np.where(reported > 0, (reported + ibnr) / np.where(reported > 0, reported, 1.0), 1.0),
        "ultimate": reported + ibnr,
        "ibnr": ibnr,
        "ibnr_unreported": lag["ibnr_unreported"].to_numpy(),
        "ibnr_known_claims": lag["ibnr_known_claims"].to_numpy(),
        "chain_ladder_cdf": s["cdf"].to_numpy(),  # quarterly accident triangle, kept for comparison
        "chain_ladder_ibnr": s["ibnr"].to_numpy(),
    })
    prem = out["earned_premium"].where(out["earned_premium"] > 0)
    out["reported_loss_ratio"] = out["reported"] / prem
    out["ultimate_loss_ratio"] = out["ultimate"] / prem
    out["ultimate_loss_ratio_pro_rata"] = out["ultimate"] / out["earned_premium_pro_rata"].where(
        out["earned_premium_pro_rata"] > 0)
    long = tri.stack().rename("cumulative").reset_index().rename(columns={"cohort": "q", "age": "age_quarters"})
    long.insert(0, "accident_quarter", _quarter_label(long.pop("q")))
    return out, long


# --------------------------------------------------------------------------- one cut-off: the valuation date
def valuation_view(
    c: pd.DataFrame, ep: pd.DataFrame, claims: pd.DataFrame, lines: pd.DataFrame, settings: Settings
) -> pd.DataFrame:
    """Segment x method, inception to the **valuation date** (the same cut as IBNR and the triangles).

    ``earned_premium`` runs to the as-of month and earns that month whole, so its raw ITD sum mixes
    bases. Here earned premium stops at the valuation month, UPR is the valuation month-end balance,
    and incurred is losses dated on or before the valuation date from lines posted by then.
    """
    val_q = valuation_quarter(settings.as_of)
    val_end = pd.Timestamp(quarter_end(val_q))
    val_key = val_end.year * 100 + val_end.month
    e = ep[ep["posting_period_key"] <= val_key]
    amt = claim_amounts(claims, lines, val_end.date())
    amt = amt[(amt["loss_date"] <= val_end) & amt["contract_id"].isin(c["contract_id"])]
    amt = amt.assign(category_code=amt["contract_id"].map(c.set_index("contract_id")["category_code"]))
    frames = []
    for seg in [ALL, *sorted(c["category_code"].dropna().unique())]:
        es = e if seg == ALL else e[e["category_code"] == seg]
        inc = float((amt if seg == ALL else amt[amt["category_code"] == seg])["incurred"].sum())
        g = es.groupby("method").agg(earned_premium=("earned_premium", "sum"))
        g["unearned_premium"] = es[es["posting_period_key"] == val_key].groupby("method")["unearned_premium"].sum()
        g = g.reindex(METHODS).fillna(0.0).reset_index()
        frames.append(g.assign(segment=str(seg), incurred_losses=inc))
    out = pd.concat(frames, ignore_index=True)
    out["loss_ratio"] = out["incurred_losses"] / out["earned_premium"].where(out["earned_premium"] > 0)
    out["valuation_date"] = val_end
    out["valuation_period_key"] = val_key
    return out[["segment", "method", "valuation_date", "valuation_period_key", "earned_premium",
                "incurred_losses", "loss_ratio", "unearned_premium"]]


def premium_deficiency(val_view: pd.DataFrame, ibnr: pd.DataFrame, settings: Settings) -> pd.DataFrame:
    """Per segment: is the selected UPR enough for the expected losses (+ expense) on unexpired coverage?

    PDR = max(0, unexpired-risk losses + maintenance expense - UPR). The flag is set when it is above 0.
    """
    upr = val_view[val_view["method"] == "selected"].set_index("segment")["unearned_premium"]
    url = ibnr.groupby("segment")["unexpired_risk_losses"].sum()
    out = pd.DataFrame({"unearned_premium": upr}).join(url.rename("unexpired_risk_losses"), how="outer").fillna(0.0)
    out["maintenance_expense"] = out["unexpired_risk_losses"] * settings.maintenance_expense_ratio
    need = out["unexpired_risk_losses"] + out["maintenance_expense"]
    out["unexpired_to_upr"] = out["unexpired_risk_losses"] / out["unearned_premium"].where(out["unearned_premium"] > 0)
    out["cost_to_upr"] = need / out["unearned_premium"].where(out["unearned_premium"] > 0)
    out["premium_deficiency_reserve"] = (need - out["unearned_premium"]).clip(lower=0)
    out["is_deficient"] = out["premium_deficiency_reserve"] > 0
    out["maintenance_expense_ratio"] = settings.maintenance_expense_ratio
    out = out.rename_axis("segment").reset_index()
    order = (out["segment"] == ALL).astype(int)  # ALL last
    return out.assign(_o=order).sort_values(["_o", "segment"]).drop(columns="_o").reset_index(drop=True)


# --------------------------------------------------------------------------- is the IBNR the right size?
def report_lag_stats(claims: pd.DataFrame, as_of: date) -> dict[str, float]:
    """Observed report lag (reported - loss date, days) on claims reported by ``as_of``."""
    cl = claims.dropna(subset=["loss_date", "reported_date"])
    cl = cl[cl["reported_date"] <= pd.Timestamp(as_of)]
    lag = (cl["reported_date"] - cl["loss_date"]).dt.days.clip(lower=0)
    if lag.empty:
        return {"report_lag_mean_days": float("nan"), "report_lag_share_within_14d": float("nan"),
                "report_lag_share_30d_plus": float("nan")}
    return {"report_lag_mean_days": float(lag.mean()),
            "report_lag_share_within_14d": float((lag <= 14).mean()),
            "report_lag_share_30d_plus": float((lag >= 30).mean())}


RUNOFF_LAG_MATURE_DAYS = 180  # lag distribution from losses at least this old (so long lags are not cut off)


def runoff_expected_share(claims: pd.DataFrame, val_end: pd.Timestamp, window_days: float) -> float:
    """Share of the IBNR claim count a ``window_days`` window after valuation should already see.

    With a steady flow of losses and report lag ``L``, the claims still unreported at valuation number
    ``E[L]`` per day of losses and those reported in the next ``W`` days ``E[min(L, W)]``, so the
    expected share is ``E[min(L, W)] / E[L]``. ``L`` comes from claims with a loss at least
    ``RUNOFF_LAG_MATURE_DAYS`` before valuation and reported by it (count weighted).
    """
    cl = claims.dropna(subset=["loss_date", "reported_date"])
    cl = cl[(cl["loss_date"] <= val_end - pd.Timedelta(days=RUNOFF_LAG_MATURE_DAYS))
            & (cl["reported_date"] <= val_end)]
    lag = (cl["reported_date"] - cl["loss_date"]).dt.days.clip(lower=0).astype(float)
    if lag.empty or lag.mean() <= 0:
        return 1.0
    return float(np.minimum(lag, window_days).mean() / lag.mean())


def ibnr_runoff(
    claims: pd.DataFrame, lines: pd.DataFrame, settings: Settings, ibnr: float | None = None
) -> dict[str, float | str]:
    """The IBNR back-test: what emerged after valuation on losses dated on or before it.

    IBNR is scored **only** against claims first reported after valuation (``ibnr_runoff_new_claims``).
    Development on claims already known at valuation (case reserves released on later denials,
    settlements below the reserve) is a case-reserve adequacy question, reported apart as
    ``case_development_known_claims`` (split into claims now denied and the rest). Netting the two would
    hide how much of the IBNR has already shown up.

    With ``ibnr``: ``ibnr_runoff_ratio`` = new claims / IBNR (actual vs expected at ultimate);
    ``ibnr_runoff_expected_share`` = the share a window this long should see (``runoff_expected_share``);
    ``ibnr_runoff_ae_to_date`` = new claims / (IBNR x that share); ``ibnr_runoff_flag`` = ``ibnr_short``
    when the to-date A/E is already above 1 (more emerged than this window should bring).
    """
    val_end = pd.Timestamp(quarter_end(valuation_quarter(settings.as_of)))
    as_of = pd.Timestamp(settings.as_of)
    window = float((as_of - val_end).days)
    cl = claims.dropna(subset=["loss_date"]).set_index("claim_id")
    ln = lines[(lines["posted_date"] > val_end) & (lines["posted_date"] <= as_of)]
    ln = ln[ln["claim_id"].map(cl["loss_date"]) <= val_end]
    late = ln["claim_id"].map(cl["reported_date"]) > val_end
    if "status_group" in cl:
        denied = ln["claim_id"].map(cl["status_group"]).eq("denied")
    else:
        denied = pd.Series(False, index=ln.index)
    new = float(ln.loc[late, "incurred"].sum())
    out: dict[str, float | str] = {
        "ibnr_runoff_window_days": window,
        "ibnr_runoff_new_claims": new,
        "ibnr_runoff_new_claim_count": float(ln.loc[late, "claim_id"].nunique()),
        "case_development_known_claims": float(ln.loc[~late, "incurred"].sum()),
        "case_development_known_denied": float(ln.loc[~late & denied, "incurred"].sum()),
        "case_development_known_other": float(ln.loc[~late & ~denied, "incurred"].sum()),
    }
    if ibnr is not None:
        share = runoff_expected_share(claims, val_end, window)
        ratio = new / ibnr if ibnr else float("nan")
        ae = new / (ibnr * share) if ibnr and share else float("nan")
        out.update({
            "ibnr_runoff_ratio": ratio,
            "ibnr_runoff_expected_share": share,
            "ibnr_runoff_ae_to_date": ae,
            "ibnr_runoff_flag": "ibnr_short" if np.isfinite(ae) and ae > 1 else "ok",
        })
    return out


def ibnr_days_of_incurred(ibnr_total: float, acc: pd.DataFrame, settings: Settings) -> float:
    """IBNR expressed as days of incurred losses (last 4 accident quarters' ultimate / 365)."""
    val_q = valuation_quarter(settings.as_of)
    q = _quarter_index(pd.to_datetime(acc["quarter_start"]))
    annual = float(acc.loc[(q > val_q - 4) & (q <= val_q), "ultimate"].sum())
    return ibnr_total / (annual / 365.0) if annual > 0 else float("nan")
