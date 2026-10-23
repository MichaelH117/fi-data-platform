"""Premium earning curves, unearned premium reserve and cancellation refunds.

Notation: ``n`` = term (months), ``k`` = months elapsed (may be fractional), ``P`` = premium.
Every function accepts scalars or numpy arrays and returns the same shape.

- pro-rata:            E(k) = k / n
- Rule of 78s:         E(k) = k (2n - k + 1) / (n (n + 1))   front-loaded
- reverse Rule of 78s: E(k) = k (k + 1) / (n (n + 1))        back-loaded

Mileage-limited terms earn on the "greater of" time and mileage: the mileage fraction is turned
into equivalent months, ``k_eff = max(k, n * miles_used / term_miles)``, and fed to the same curve.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
from numpy.typing import ArrayLike, NDArray

Curve = Callable[[NDArray[np.float64], NDArray[np.float64]], NDArray[np.float64]]


def _pro_rata(k: NDArray[np.float64], n: NDArray[np.float64]) -> NDArray[np.float64]:
    return k / n


def _rule_of_78s(k: NDArray[np.float64], n: NDArray[np.float64]) -> NDArray[np.float64]:
    return k * (2 * n - k + 1) / (n * (n + 1))


def _reverse_rule_of_78s(k: NDArray[np.float64], n: NDArray[np.float64]) -> NDArray[np.float64]:
    return k * (k + 1) / (n * (n + 1))


CURVES: dict[str, Curve] = {
    "pro_rata": _pro_rata,
    "rule_of_78s": _rule_of_78s,
    "reverse_rule_of_78s": _reverse_rule_of_78s,
}


def earned_fraction(
    k: ArrayLike,
    n: ArrayLike,
    curve: str = "pro_rata",
    miles_used: ArrayLike | None = None,
    term_miles: ArrayLike | None = None,
) -> NDArray[np.float64]:
    """Fraction of premium earned after ``k`` of ``n`` months, clipped to [0, 1].

    Pass ``miles_used`` and ``term_miles`` for a mileage-limited contract to earn on the greater of
    elapsed time and elapsed mileage.
    """
    if curve not in CURVES:
        raise ValueError(f"unknown curve {curve!r}; choose one of {sorted(CURVES)}")
    k_arr = np.asarray(k, dtype=float)
    n_arr = np.asarray(n, dtype=float)
    if np.any(n_arr <= 0):
        raise ValueError("term n must be positive")
    if (miles_used is None) != (term_miles is None):
        raise ValueError("pass both miles_used and term_miles, or neither")
    if miles_used is not None:
        k_miles = n_arr * np.asarray(miles_used, dtype=float) / np.asarray(term_miles, dtype=float)
        k_arr = np.maximum(k_arr, k_miles)
    k_arr = np.clip(k_arr, 0.0, n_arr)
    return np.clip(CURVES[curve](k_arr, n_arr), 0.0, 1.0)


def unearned_premium(
    premium: ArrayLike, k: ArrayLike, n: ArrayLike, curve: str = "pro_rata", **mileage: ArrayLike
) -> NDArray[np.float64]:
    """Unearned premium reserve: ``P * (1 - E(k))``. Sum the result for a portfolio UPR."""
    return np.asarray(premium, dtype=float) * (1.0 - earned_fraction(k, n, curve, **mileage))


def cancellation_refund(
    retail_price: ArrayLike,
    elapsed: ArrayLike,
    term: ArrayLike,
    fee: float = 50.0,
    curve: str = "pro_rata",
) -> NDArray[np.float64]:
    """Customer refund on cancellation: ``max(0, price * (1 - E(elapsed)) - fee)``, rounded to cents.

    ``elapsed`` and ``term`` only need to share a unit (days or months). Pro-rata is the default
    because most states require it for consumer refunds.
    """
    unearned = np.asarray(retail_price, dtype=float) * (1.0 - earned_fraction(elapsed, term, curve))
    return np.round(np.maximum(unearned - fee, 0.0), 2)


# --------------------------------------------------------------------------- V3 earning methods
# Named methods the pipeline computes for every contract. ``selected`` is the per-contract method
# chosen by ``actuarial/earning_methods.csv`` (custom curves live in ``actuarial/earning_curves.csv``).
METHODS: tuple[str, ...] = ("pro_rata", "pro_rata_mileage", "rule_of_78s", "reverse_rule_of_78s", "selected")
ASSIGNABLE = ("pro_rata", "pro_rata_mileage", "rule_of_78s", "reverse_rule_of_78s", "custom")


def mileage_months(
    k: ArrayLike, n: ArrayLike, monthly_miles: ArrayLike, term_miles: ArrayLike
) -> NDArray[np.float64]:
    """Equivalent elapsed months on the "greater of" time and mileage, clipped to ``[0, n]``.

    ``miles_used = monthly_miles * k``; a contract with no mileage limit (``term_miles`` missing or 0)
    earns on time alone. Feed the result to any curve, e.g. ``k / n`` for mileage-aware pro-rata.
    """
    k_arr = np.asarray(k, dtype=float)
    n_arr = np.asarray(n, dtype=float)
    tm = np.asarray(term_miles, dtype=float)
    has_limit = np.isfinite(tm) & (tm > 0)
    k_miles = np.where(has_limit, n_arr * np.asarray(monthly_miles, dtype=float) * k_arr / np.where(has_limit, tm, 1.0),
                       0.0)
    return np.clip(np.maximum(k_arr, np.nan_to_num(k_miles)), 0.0, n_arr)


def validate_curve(term_pct: ArrayLike, earned_pct: ArrayLike) -> None:
    """A custom curve must run from (0, 0) to (1, 1) and never decrease."""
    t, e = np.asarray(term_pct, dtype=float), np.asarray(earned_pct, dtype=float)
    if len(t) < 2 or len(t) != len(e):
        raise ValueError("a custom curve needs at least two (term_pct, earned_pct) knots")
    if t[0] != 0 or e[0] != 0 or t[-1] != 1 or e[-1] != 1:
        raise ValueError("a custom curve must start at (0, 0) and end at (1, 1)")
    if np.any(np.diff(t) <= 0) or np.any(np.diff(e) < 0):
        raise ValueError("custom curve knots must have increasing term_pct and non-decreasing earned_pct")


def custom_fraction(k: ArrayLike, n: ArrayLike, term_pct: ArrayLike, earned_pct: ArrayLike) -> NDArray[np.float64]:
    """Earned fraction from a piecewise-linear custom curve: ``E(k) = interp(k / n, term_pct, earned_pct)``."""
    validate_curve(term_pct, earned_pct)
    x = np.clip(np.asarray(k, dtype=float) / np.asarray(n, dtype=float), 0.0, 1.0)
    return np.interp(x, np.asarray(term_pct, dtype=float), np.asarray(earned_pct, dtype=float))
