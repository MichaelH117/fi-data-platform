"""Loss-development triangles, chain-ladder and Bornhuetter-Ferguson reserving.

A triangle is a ``DataFrame``: one row per cohort (origin period), one column per development
age (1, 2, ...), holding **cumulative** losses, with NaN in cells not yet observed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd


def build_triangle(
    incremental: pd.DataFrame,
    latest_age: Mapping[object, int],
    cohort: str = "cohort",
    age: str = "age",
    value: str = "value",
) -> pd.DataFrame:
    """Turn long incremental rows into a cumulative triangle.

    ``latest_age`` maps every cohort to its last observed age at the valuation date; that decides
    the diagonal, so cohorts or ages with no losses still show as 0 (observed) rather than NaN.
    Rows at an age beyond their cohort's ``latest_age`` are rejected: they cannot exist yet.
    """
    if len(incremental) and incremental[age].min() < 1:
        raise ValueError("ages start at 1")
    late = incremental[age] > incremental[cohort].map(latest_age)
    if late.any():
        raise ValueError(f"{int(late.sum())} rows lie beyond their cohort's valuation age")
    cohorts = sorted(latest_age)
    ages = range(1, max(latest_age.values()) + 1)
    wide = (
        incremental.pivot_table(index=cohort, columns=age, values=value, aggfunc="sum")
        .reindex(index=cohorts, columns=ages)
        .fillna(0.0)
        .cumsum(axis=1)
    )
    observed = np.array(ages)[None, :] <= np.array([latest_age[c] for c in cohorts])[:, None]
    tri = wide.where(observed)
    tri.index.name, tri.columns.name = cohort, age
    return tri


def link_cohorts(
    triangle: pd.DataFrame, a: int, b: int, from_period: int | None = None, min_cohorts: int = 1
) -> pd.Series:
    """Which cohorts feed the ``a -> b`` link ratio (a boolean mask over the triangle's rows).

    Every cohort observed at both ages, or, with ``from_period``, only the links that start on a
    calendar diagonal at or after it: cohort ``i`` counts when ``i + a - 1 >= from_period`` (cohorts
    and ages are period indexes, age 1 = the origin period). That keeps development that happened
    under an old process (for example before a system migration that changed how fast losses post)
    out of the factors, while old cohorts still inform the late ages through their recent diagonals.
    When fewer than ``min_cohorts`` cohorts qualify, or their losses at ``a`` are 0, the link falls back
    to every cohort.
    """
    both = triangle[a].notna() & triangle[b].notna()
    if from_period is None:
        return both
    origin = pd.Series(np.asarray(triangle.index, dtype=float), index=triangle.index)
    recent = both & (origin + a - 1 >= from_period)
    if recent.sum() >= max(min_cohorts, 1) and triangle.loc[recent, a].sum():
        return recent
    return both


def age_to_age_factors(
    triangle: pd.DataFrame, from_period: int | None = None, min_cohorts: int = 1
) -> pd.Series:
    """Volume-weighted link ratios ``f_j = sum_i C[i, j+1] / sum_i C[i, j]``.

    Only cohorts observed at both ages contribute (and, with ``from_period``, only links on diagonals
    from that period on; see ``link_cohorts``). Indexed by the *from* age; an age with zero losses
    in the denominator gets a factor of 1.0.
    """
    ages = list(triangle.columns)
    factors = {}
    for a, b in zip(ages[:-1], ages[1:], strict=True):
        use = link_cohorts(triangle, a, b, from_period, min_cohorts)
        den = triangle.loc[use, a].sum()
        factors[a] = triangle.loc[use, b].sum() / den if den else 1.0
    return pd.Series(factors, name="ata", dtype=float).rename_axis("age")


def link_cohort_counts(
    triangle: pd.DataFrame, from_period: int | None = None, min_cohorts: int = 1
) -> pd.Series:
    """How many cohorts each age-to-age factor was fitted on (same rule as ``age_to_age_factors``)."""
    ages = list(triangle.columns)
    n = {a: int(link_cohorts(triangle, a, b, from_period, min_cohorts).sum())
         for a, b in zip(ages[:-1], ages[1:], strict=True)}
    return pd.Series(n, name="cohorts", dtype=int).rename_axis("age")


def cumulative_factors(ata: pd.Series, tail: float = 1.0) -> pd.Series:
    """Cumulative development factors to ultimate: ``CDF_j = f_j * f_{j+1} * ... * tail``.

    The result has one entry per age including the last one (whose CDF is just the tail).
    """
    last_age = (ata.index.max() + 1) if len(ata) else 1
    with_tail = pd.concat([ata, pd.Series({last_age: tail})])
    cdf = with_tail[::-1].cumprod()[::-1]
    return cdf.rename("cdf").rename_axis("age")


@dataclass(frozen=True)
class ChainLadderResult:
    ata: pd.Series
    cdf: pd.Series
    summary: pd.DataFrame  # per cohort: age, latest, cdf, ultimate, ibnr

    @property
    def total_ibnr(self) -> float:
        return float(self.summary["ibnr"].sum())


def latest_diagonal(triangle: pd.DataFrame) -> pd.DataFrame:
    """Latest observed age and cumulative value per cohort."""
    age = triangle.notna().to_numpy().cumsum(axis=1).argmax(axis=1)
    ages = np.array(triangle.columns)[age]
    values = triangle.to_numpy()[np.arange(len(triangle)), age]
    return pd.DataFrame({"age": ages, "latest": values}, index=triangle.index)


def chain_ladder(
    triangle: pd.DataFrame, tail: float = 1.0, from_period: int | None = None, min_cohorts: int = 1
) -> ChainLadderResult:
    """Basic chain-ladder: ultimate = latest x CDF at the latest age; IBNR = ultimate - latest.

    ``from_period`` / ``min_cohorts`` choose which links fit the factors (see ``link_cohorts``).
    """
    ata = age_to_age_factors(triangle, from_period, min_cohorts)
    cdf = cumulative_factors(ata, tail)
    out = latest_diagonal(triangle)
    out["cdf"] = out["age"].map(cdf)
    out["ultimate"] = out["latest"] * out["cdf"]
    out["ibnr"] = out["ultimate"] - out["latest"]
    return ChainLadderResult(ata=ata, cdf=cdf, summary=out)


def bornhuetter_ferguson(
    latest: pd.Series, cdf: pd.Series, premium: pd.Series, expected_loss_ratio: float | pd.Series
) -> pd.DataFrame:
    """Bornhuetter-Ferguson: ``IBNR = ELR x premium x (1 - 1 / CDF)``; ultimate = latest + IBNR.

    It trusts an a-priori loss ratio for the part of losses not yet reported, so a young cohort
    with an unlucky (or lucky) first few claims is not multiplied by a huge CDF.
    """
    pct_unreported = 1.0 - 1.0 / cdf
    ibnr = expected_loss_ratio * premium * pct_unreported
    return pd.DataFrame({"pct_unreported": pct_unreported, "ibnr": ibnr, "ultimate": latest + ibnr})
