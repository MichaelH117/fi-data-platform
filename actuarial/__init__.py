"""Actuarial library: earning methods, UPR, refunds, loss triangles, IBNR, early claims, LR forecast.

The math lives in pure modules with no I/O (``earning``, ``reserving``, ``emergence``, ``forecast``),
so it can be unit tested against hand-worked examples. ``data`` reads the ``core`` marts, ``build``
assembles the tables, ``__main__`` writes schema ``actuarial`` and ``report`` is the site plug-in.
"""

from actuarial.earning import (
    CURVES,
    METHODS,
    cancellation_refund,
    custom_fraction,
    earned_fraction,
    mileage_months,
    unearned_premium,
)
from actuarial.reserving import (
    ChainLadderResult,
    age_to_age_factors,
    bornhuetter_ferguson,
    build_triangle,
    chain_ladder,
    cumulative_factors,
)

__all__ = [
    "CURVES",
    "METHODS",
    "ChainLadderResult",
    "age_to_age_factors",
    "bornhuetter_ferguson",
    "build_triangle",
    "cancellation_refund",
    "chain_ladder",
    "cumulative_factors",
    "custom_fraction",
    "earned_fraction",
    "mileage_months",
    "unearned_premium",
]
