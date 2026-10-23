"""Cell suppression for aggregate peer benchmarks.

The input is a full additive cube: one row per (period, dim_1 .. dim_n) where any dimension may be
``ALL`` (the margin). Each row carries ``contracts``, ``dealers`` and ``max_dealer_share`` computed
from dealer-level contributions, so the rules below can be checked on every cell, margins included.

Rules (see docs/REPORTS.md, "Publish layer"):

1. **Primary suppression.** A cell is withheld when it has fewer than ``min_contracts`` contracts,
   fewer than ``min_dealers`` contributing dealers, or one dealer supplies more than
   ``max_dealer_share`` of it (dominance rule).
2. **Complementary suppression.** Every margin is a published total of its children, so a lone
   suppressed member of a margin group could be recovered by subtraction. Each group must have
   zero or at least two suppressed members; the smallest visible member is added until that holds.
3. **Exact-disclosure audit.** Groups overlap (a cell sits in one group per dimension), so the
   pairwise rule is followed by a linear-algebra check: a suppressed cell is *disclosed* if its value
   is a linear combination of the published equations. Any disclosed cell triggers one more
   complementary suppression, and the audit repeats until nothing can be solved for.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd

ALL = "ALL"


@dataclass(frozen=True)
class Rules:
    min_contracts: int = 11
    min_dealers: int = 5
    max_dealer_share: float = 0.25


def primary_reason(contracts: int, dealers: int, max_share: float, rules: Rules) -> str:
    """The first primary rule a cell breaks, or '' when the cell may be published."""
    if contracts < rules.min_contracts:
        return "min_contracts"
    if dealers < rules.min_dealers:
        return "min_dealers"
    if max_share > rules.max_dealer_share:
        return "dominance"
    return ""


@dataclass(frozen=True)
class Group:
    """One additive relation: ``sum(children) == margin`` (indexes into the period's rows)."""

    children: tuple[int, ...]
    margin: int

    @property
    def members(self) -> tuple[int, ...]:
        return (*self.children, self.margin)


def margin_groups(cells: pd.DataFrame, dims: Sequence[str]) -> list[Group]:
    """Every additive relation in one period of a cube.

    For each dimension d and each fixed value of the other dimensions, the children are the rows
    with d != ALL and the margin is the row with d == ALL.
    """
    groups: list[Group] = []
    for d in dims:
        others = [o for o in dims if o != d]
        keyed = cells.groupby(others, sort=False) if others else [((), cells)]
        for _, g in keyed:
            margin = g.index[g[d] == ALL]
            children = tuple(int(i) for i in g.index[g[d] != ALL])
            if len(margin) == 1 and children:
                groups.append(Group(children, int(margin[0])))
    return groups


def disclosed_cells(groups: Sequence[Group], suppressed: set[int]) -> list[int]:
    """Suppressed cells whose value is uniquely determined by the published numbers.

    Each group gives ``sum(children) - margin == 0``. Moving the published members to the right-hand
    side leaves a linear system in the suppressed members only; cell j is solvable exactly when the
    unit vector e_j lies in the row space of that system, i.e. when row j of a null-space basis of
    the system is zero (one SVD for all cells instead of one rank computation per cell).
    """
    hidden = sorted(suppressed)
    col = {idx: i for i, idx in enumerate(hidden)}
    rows = []
    for g in groups:
        row = np.zeros(len(hidden))
        for m in g.children:
            if m in col:
                row[col[m]] = 1.0
        if g.margin in col:
            row[col[g.margin]] = -1.0
        if row.any():
            rows.append(row)
    if not rows:
        return []
    a = np.vstack(rows)
    # null(a) = null(a^T a); the symmetric eigensolver is robust where a full SVD may not converge
    w, v = np.linalg.eigh(a.T @ a)
    tol = max(a.shape) * np.finfo(float).eps * max(float(w[-1]), 1.0) * 10
    null_basis = v[:, w <= tol]  # columns span the null space of a
    if null_basis.shape[1] == 0:
        return list(col)
    solvable = np.abs(null_basis).max(axis=1) < 1e-9
    return [idx for idx, j in col.items() if solvable[j]]


def suppress_period(cells: pd.DataFrame, dims: Sequence[str], rules: Rules) -> pd.Series:
    """Return a suppression reason per row ('' = published) for one period of a cube."""
    reason = pd.Series(
        [
            primary_reason(int(c), int(n), float(s), rules)
            for c, n, s in zip(cells["contracts"], cells["dealers"], cells["max_dealer_share"], strict=True)
        ],
        index=cells.index,
        dtype=object,
    )
    groups = margin_groups(cells, dims)
    size = cells["contracts"]

    def add_smallest(candidates: Iterable[int]) -> bool:
        visible = [i for i in candidates if reason[i] == ""]
        if not visible:
            return False
        reason[min(visible, key=lambda i: (size[i], str(i)))] = "complementary"
        return True

    while True:
        changed = False
        for g in groups:  # rule 2: no group with exactly one hidden member
            if sum(reason[i] != "" for i in g.members) == 1:
                changed |= add_smallest(g.members)
        if changed:
            continue
        leaks = disclosed_cells(groups, {i for i in cells.index if reason[i] != ""})
        if not leaks:  # rule 3: nothing can be solved for
            return reason
        touching = [i for g in groups if leaks[0] in g.members for i in g.members]
        if not add_smallest(touching) and not add_smallest(cells.index):
            return reason  # everything is hidden


def apply_suppression(cube: pd.DataFrame, dims: Sequence[str], period: str, rules: Rules) -> pd.DataFrame:
    """Suppress a whole cube (all periods) and blank every measure in withheld cells.

    ``cube`` must contain ``period``, ``dims``, ``contracts``, ``dealers``, ``max_dealer_share``.
    Returns the cube with ``suppressed`` and ``suppression_reason`` added, the helper columns
    ``dealers`` / ``max_dealer_share`` dropped, and every other measure set to NA where suppressed.
    """
    out = cube.reset_index(drop=True).copy()
    out["suppression_reason"] = ""
    for _, part in out.groupby(period, sort=False):
        out.loc[part.index, "suppression_reason"] = suppress_period(part, dims, rules)
    out["suppressed"] = out["suppression_reason"] != ""
    measures = [c for c in out.columns if c not in {period, *dims, "suppressed", "suppression_reason"}]
    for m in measures:
        out[m] = out[m].astype("Int64" if pd.api.types.is_integer_dtype(out[m]) else "Float64")
    out.loc[out["suppressed"], measures] = pd.NA
    return out.drop(columns=["dealers", "max_dealer_share"])
