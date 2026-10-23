"""Early-claim emergence: how many contracts have a first claim within 30, 60, ... 180 days.

Early claims are an underwriting and fraud signal (a pre-existing condition, a vehicle sold with a
known fault, or a claim inside the waiting period). A contract counts in a window only once it has
been effective for the whole window ("exposed"), so young contracts never drag a rate down.
``early_claim_contracts`` ignores denied claims, the same rule as the ``is_early_claim_<N>`` flags in
``core.dim_contract_claim_summary``. ``early_reported_contracts`` counts denied claims too, because an
early claim is a signal even when it is not paid; the dealer watchlist uses that one.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

import numpy as np
import pandas as pd

WINDOWS: tuple[int, ...] = (30, 60, 90, 120, 150, 180)
SEGMENT = ["category_code", "subcategory_code", "new_used"]


def first_claim_days(
    contracts: pd.DataFrame, claims: pd.DataFrame, as_of: date, include_denied: bool = False
) -> pd.Series:
    """Days from effective date to the first loss on or after it (NaN if none by ``as_of``)."""
    eff = contracts.set_index("contract_id")["effective_date"]
    k = claims.dropna(subset=["contract_id", "loss_date"])
    if not include_denied and "status_group" in k:
        k = k[k["status_group"] != "denied"]
    k = k[k["contract_id"].isin(eff.index) & (k["loss_date"] <= pd.Timestamp(as_of))]
    days = (k["loss_date"] - k["contract_id"].map(eff)).dt.days
    first = days[days >= 0].groupby(k.loc[days >= 0, "contract_id"]).min()
    return eff.index.to_series().map(first).rename("first_claim_days")


def _exposure(contracts: pd.DataFrame, claims: pd.DataFrame, as_of: date, windows: Sequence[int]) -> pd.DataFrame:
    c = contracts.reset_index(drop=True)
    days = first_claim_days(c, claims, as_of).to_numpy()
    days_all = first_claim_days(c, claims, as_of, include_denied=True).to_numpy()
    age = (pd.Timestamp(as_of) - c["effective_date"]).dt.days.to_numpy()
    wait = c["claim_wait_days"].fillna(0).to_numpy(dtype=float) if "claim_wait_days" in c else np.zeros(len(c))
    frames = []
    for w in windows:
        exposed = age >= w
        early = exposed & (days <= w)  # NaN compares False
        frames.append(c.assign(
            window_days=w,
            exposed=exposed.astype(int),
            early=early.astype(int),
            early_all=(exposed & (days_all <= w)).astype(int),
            in_wait=(early & (days <= wait)).astype(int),
        ))
    return pd.concat(frames, ignore_index=True)


def early_claim_emergence(
    contracts: pd.DataFrame, claims: pd.DataFrame, as_of: date, windows: Sequence[int] = WINDOWS
) -> pd.DataFrame:
    """Effective-quarter cohort x product segment x window: exposed, early-claim contracts, rate."""
    x = _exposure(contracts, claims, as_of, windows)
    q = x["effective_date"].dt.year * 4 + (x["effective_date"].dt.month - 1) // 3
    x["cohort_label"] = (q // 4).astype(str) + "Q" + (q % 4 + 1).astype(str)
    x[SEGMENT] = x[SEGMENT].fillna("UNKNOWN")
    g = x.groupby(["cohort_label", *SEGMENT, "window_days"], as_index=False).agg(
        contracts_exposed=("exposed", "sum"),
        early_claim_contracts=("early", "sum"),
        early_reported_contracts=("early_all", "sum"),
        within_wait_period=("in_wait", "sum"),
    )
    g = g[g["contracts_exposed"] > 0]
    g["early_claim_rate"] = g["early_claim_contracts"] / g["contracts_exposed"]
    return g.reset_index(drop=True)


def early_claim_by_dealer(
    contracts: pd.DataFrame,
    claims: pd.DataFrame,
    as_of: date,
    windows: Sequence[int] = (90, 180),
    min_exposed: int = 30,
) -> pd.DataFrame:
    """Dealer x window: observed vs expected early claims (actual-to-expected, A/E), denied included.

    Expected uses the portfolio rate of each contract's own product segment (subcategory x new/used),
    so a dealer that sells riskier products is not flagged for its mix alone. ``z`` is the Poisson
    score ``(observed - expected) / sqrt(expected)``. A dealer is flagged when it has at least
    ``min_exposed`` exposed contracts, A/E >= 1.5 and z >= 2.
    """
    x = _exposure(contracts, claims, as_of, windows)
    x = x[x["exposed"] == 1]
    x[SEGMENT] = x[SEGMENT].fillna("UNKNOWN")
    rate = x.groupby(["window_days", *SEGMENT])["early_all"].transform("mean")
    x = x.assign(expected=rate)
    g = x.groupby(["dealer_number", "window_days"], as_index=False).agg(
        contracts_exposed=("exposed", "sum"),
        early_claim_contracts=("early_all", "sum"),
        expected_early_claims=("expected", "sum"),
    )
    g["early_claim_rate"] = g["early_claim_contracts"] / g["contracts_exposed"]
    g["actual_to_expected"] = g["early_claim_contracts"] / g["expected_early_claims"].where(
        g["expected_early_claims"] > 0)
    g["z_score"] = (g["early_claim_contracts"] - g["expected_early_claims"]) / np.sqrt(
        g["expected_early_claims"].where(g["expected_early_claims"] > 0))
    g["is_flagged"] = (g["contracts_exposed"] >= min_exposed) & (g["actual_to_expected"] >= 1.5) & (g["z_score"] >= 2)
    return g.sort_values(["window_days", "z_score"], ascending=[True, False]).reset_index(drop=True)
