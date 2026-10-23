"""Which earning method each contract uses: product (category / subcategory) x new/used rules.

The rules live in two small, reviewable config files next to this module:

- ``earning_methods.csv``: category_code, subcategory_code, new_used, method, curve_id, rationale.
  ``*`` is a wildcard. The most specific matching row wins (subcategory beats new/used beats category).
- ``earning_curves.csv``: curve_id, term_pct, earned_pct knots for the ``custom`` method.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from actuarial.earning import ASSIGNABLE, validate_curve

HERE = Path(__file__).resolve().parent
METHODS_CSV = HERE / "earning_methods.csv"
CURVES_CSV = HERE / "earning_curves.csv"


def load_curves(path: Path = CURVES_CSV) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    df = pd.read_csv(path).sort_values(["curve_id", "term_pct"])
    curves = {}
    for cid, g in df.groupby("curve_id"):
        t, e = g["term_pct"].to_numpy(float), g["earned_pct"].to_numpy(float)
        validate_curve(t, e)
        curves[str(cid)] = (t, e)
    return curves


def load_rules(path: Path = METHODS_CSV, curves: dict | None = None) -> pd.DataFrame:
    rules = pd.read_csv(path, dtype=str, keep_default_na=False)
    bad = sorted(set(rules["method"]) - set(ASSIGNABLE))
    if bad:
        raise ValueError(f"unknown earning method(s) {bad}; choose from {ASSIGNABLE}")
    curves = load_curves() if curves is None else curves
    missing = rules.loc[(rules["method"] == "custom") & ~rules["curve_id"].isin(list(curves)), "curve_id"]
    if len(missing):
        raise ValueError(f"custom method without a defined curve: {sorted(set(missing))}")
    if not ((rules["category_code"] == "*") & (rules["subcategory_code"] == "*") & (rules["new_used"] == "*")).any():
        raise ValueError("earning_methods.csv needs a catch-all '*,*,*' default row")
    rules["specificity"] = (
        (rules["subcategory_code"] != "*") * 4 + (rules["new_used"] != "*") * 2 + (rules["category_code"] != "*") * 1
    )
    return rules


def assign_methods(contracts: pd.DataFrame, rules: pd.DataFrame) -> pd.DataFrame:
    """Return ``method`` and ``curve_id`` per contract row (same index), most specific rule first."""
    method = pd.Series(pd.NA, index=contracts.index, dtype="object")
    curve = pd.Series("", index=contracts.index, dtype="object")
    cat = contracts["category_code"].fillna("").str.upper()
    sub = contracts["subcategory_code"].fillna("").str.upper()
    nu = contracts["new_used"].fillna("").str.upper()
    for r in rules.sort_values("specificity", ascending=False).itertuples():
        hit = method.isna()
        if r.category_code != "*":
            hit &= cat == r.category_code.upper()
        if r.subcategory_code != "*":
            hit &= sub == r.subcategory_code.upper()
        if r.new_used != "*":
            hit &= nu == r.new_used.upper()
        method[hit] = r.method
        curve[hit] = r.curve_id
    return pd.DataFrame({"method": method, "curve_id": curve})
