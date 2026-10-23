"""Publish layer: primary rules, complementary suppression and a re-identification audit."""

from __future__ import annotations

import itertools
import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from publish.build import DIMS, build_benchmarks, cell_totals
from publish.packets import Packet, build_packets, packet_tables, packets
from publish.suppression import ALL, Rules, apply_suppression, disclosed_cells, margin_groups, primary_reason

RULES = Rules()
ROOT = Path(__file__).resolve().parents[1]
WAREHOUSE = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))


# --------------------------------------------------------------------------- helpers
def cube(values: dict[tuple[str, str], tuple[int, int, float]], period: str = "2026-01") -> pd.DataFrame:
    """2-D cube (row x col) with ALL margins. values: (row, col) -> (contracts, dealers, max_share).

    Margins get summed contracts and comfortable dealer counts so only the children trip rules,
    unless a test overrides them explicitly.
    """
    rows = sorted({r for r, _ in values})
    cols = sorted({c for _, c in values})
    recs = [dict(period=period, row=r, col=c, contracts=v[0], dealers=v[1], max_dealer_share=v[2])
            for (r, c), v in values.items()]
    for r in rows:
        recs.append(dict(period=period, row=r, col=ALL, contracts=sum(values[r, c][0] for c in cols),
                         dealers=20, max_dealer_share=0.1))
    for c in cols:
        recs.append(dict(period=period, row=ALL, col=c, contracts=sum(values[r, c][0] for r in rows),
                         dealers=20, max_dealer_share=0.1))
    recs.append(dict(period=period, row=ALL, col=ALL, contracts=sum(v[0] for v in values.values()),
                     dealers=40, max_dealer_share=0.05))
    df = pd.DataFrame(recs)
    df["earned_premium"] = df["contracts"] * 100.0
    return df


def safe(contracts: int = 100) -> tuple[int, int, float]:
    return (contracts, 10, 0.15)


def determined_by_nullspace(groups, hidden: list[int]) -> set[int]:
    """Independent check of exact disclosure: j is determined iff every null-space vector is 0 at j.

    Uses a full SVD (the production code uses an eigen-decomposition), once per period.
    """
    col = {h: i for i, h in enumerate(hidden)}
    rows = []
    for g in groups:
        row = np.zeros(len(hidden))
        for m in g.children:
            if m in col:
                row[col[m]] = 1
        if g.margin in col:
            row[col[g.margin]] = -1
        rows.append(row)
    a = np.vstack(rows) if rows else np.zeros((1, len(hidden)))
    try:
        _, s, vt = np.linalg.svd(a)
    except np.linalg.LinAlgError:  # fall back to the symmetric eigensolver on a^T a
        w, v = np.linalg.eigh(a.T @ a)
        s, vt = np.sqrt(np.clip(w[::-1], 0, None)), v[:, ::-1].T
    rank = int((s > 1e-6).sum())
    null = vt[rank:]
    if not len(null):
        return set(hidden)
    return {h for h, i in col.items() if np.all(np.abs(null[:, i]) < 1e-9)}


def is_determined_by_nullspace(groups, hidden: list[int], j: int) -> bool:
    return j in determined_by_nullspace(groups, hidden)


def assert_no_disclosure(published: pd.DataFrame, dims: tuple[str, ...]) -> None:
    for _, part in published.groupby("period"):
        part = part.copy()
        groups = margin_groups(part, dims)
        hidden = [int(i) for i in part.index[part["suppressed"]]]
        hidden_set = set(hidden)
        for g in groups:
            n_hidden = sum(m in hidden_set for m in g.members)
            assert n_hidden != 1, f"group {g} has a single hidden cell (recoverable by subtraction)"
        if hidden:
            leaked = determined_by_nullspace(groups, hidden)
            assert not leaked, f"cells {sorted(leaked)[:5]} are exactly derivable"


# --------------------------------------------------------------------------- primary rules
@pytest.mark.parametrize(
    ("contracts", "dealers", "share", "expected"),
    [
        (11, 5, 0.25, ""),  # every threshold exactly at the limit is publishable
        (10, 5, 0.10, "min_contracts"),
        (0, 0, 0.0, "min_contracts"),
        (500, 4, 0.10, "min_dealers"),
        (500, 9, 0.2501, "dominance"),
        (500, 1, 1.0, "min_dealers"),
    ],
)
def test_primary_rules(contracts: int, dealers: int, share: float, expected: str) -> None:
    assert primary_reason(contracts, dealers, share, RULES) == expected


def test_rules_are_configurable() -> None:
    strict = Rules(min_contracts=50, min_dealers=8, max_dealer_share=0.2)
    assert primary_reason(40, 10, 0.1, strict) == "min_contracts"
    assert primary_reason(60, 7, 0.1, strict) == "min_dealers"
    assert primary_reason(60, 10, 0.21, strict) == "dominance"


# --------------------------------------------------------------------------- complementary suppression
def test_margin_groups_cover_every_row_and_column() -> None:
    df = cube({(r, c): safe() for r in "ab" for c in "xyz"})
    groups = margin_groups(df, ("row", "col"))
    # 2 rows + ALL row -> 3 column-wise groups (over col); 3 cols + ALL col -> 4 row-wise groups.
    assert len(groups) == 3 + 4
    assert all(len(g.children) in (2, 3) for g in groups)


def test_single_small_cell_gets_a_complement_in_its_row_and_column() -> None:
    values = {(r, c): safe(100 + 10 * i) for i, (r, c) in enumerate(itertools.product("abc", "xyz"))}
    values["a", "x"] = (5, 10, 0.15)  # the only primary suppression
    out = apply_suppression(cube(values), ("row", "col"), "period", RULES)
    hidden = out[out["suppressed"]]
    assert set(hidden["suppression_reason"]) == {"min_contracts", "complementary"}
    assert ((hidden["row"] == "a") & (hidden["col"] != ALL)).sum() >= 2
    assert ((hidden["col"] == "x") & (hidden["row"] != ALL)).sum() >= 2
    assert_no_disclosure(out, ("row", "col"))


def test_complement_is_the_smallest_visible_cell() -> None:
    values = {("a", "x"): (3, 10, 0.1), ("a", "y"): (500, 10, 0.1), ("a", "z"): (40, 10, 0.1),
              ("b", "x"): (60, 10, 0.1), ("b", "y"): (700, 10, 0.1), ("b", "z"): (45, 10, 0.1)}
    out = apply_suppression(cube(values), ("row", "col"), "period", RULES).set_index(["row", "col"])
    assert out.loc[("a", "z"), "suppression_reason"] == "complementary"  # 40 < 500 in row a
    assert out.loc[("b", "x"), "suppression_reason"] == "complementary"  # 60 < 700 in row b
    assert not out.loc[("a", "y"), "suppressed"]


def test_suppressed_margin_is_protected_too() -> None:
    values = {(r, c): safe() for r in "ab" for c in "xy"}
    df = cube(values)
    margin = (df["row"] == "a") & (df["col"] == ALL)
    df.loc[margin, "max_dealer_share"] = 0.6  # the row total is dominated by one dealer
    out = apply_suppression(df, ("row", "col"), "period", RULES)
    assert out.loc[margin, "suppression_reason"].item() == "dominance"
    assert out["suppressed"].sum() >= 2
    assert_no_disclosure(out, ("row", "col"))


def test_one_child_group_hides_child_and_margin_together() -> None:
    # A row with a single child: the margin equals the child, so hiding one must hide both.
    values = {("a", "x"): (5, 10, 0.1), ("b", "x"): safe(), ("b", "y"): safe(), ("a", "y"): safe(200)}
    df = cube(values)
    df = df[~((df["row"] == "a") & (df["col"] == "y"))].reset_index(drop=True)
    df.loc[(df["row"] == "a") & (df["col"] == ALL), "contracts"] = 5
    out = apply_suppression(df, ("row", "col"), "period", RULES)
    a = out[out["row"] == "a"]
    assert a["suppressed"].all()
    assert_no_disclosure(out, ("row", "col"))


@pytest.mark.parametrize("seed", range(40))
def test_random_cubes_never_disclose_a_hidden_cell(seed: int) -> None:
    """Property test on 3-D cubes like the real one: no hidden cell can be solved from the margins."""
    rng = np.random.default_rng(seed)
    dims = ("state", "tier", "band")
    levels = {"state": list("ABCD")[: rng.integers(2, 5)], "tier": ["P", "G", "X"], "band": ["s", "m", "l"]}
    leaf = {k: (int(rng.integers(0, 60)), int(rng.integers(1, 12)), float(rng.uniform(0.05, 0.5)))
            for k in itertools.product(*levels.values())}
    recs = []
    for combo in itertools.product(*[[*levels[d], ALL] for d in dims]):
        match = [v for k, v in leaf.items() if all(c in (ALL, kk) for c, kk in zip(combo, k, strict=True))]
        recs.append(dict(zip(dims, combo, strict=True), period="p", contracts=sum(m[0] for m in match),
                         dealers=max(m[1] for m in match) + len(match) // 3,
                         max_dealer_share=min(m[2] for m in match)))
    out = apply_suppression(pd.DataFrame(recs), dims, "period", RULES)
    visible = out[~out["suppressed"]]
    assert (visible["contracts"] >= RULES.min_contracts).all()
    assert_no_disclosure(out, dims)


def test_disclosed_cells_flags_a_lone_hidden_child() -> None:
    df = cube({(r, c): safe() for r in "ab" for c in "xy"})
    groups = margin_groups(df, ("row", "col"))
    lone = int(df.index[(df["row"] == "a") & (df["col"] == "x")][0])
    assert disclosed_cells(groups, {lone}) == [lone]
    # A 2x2 rectangle of hidden children is the classic safe pattern.
    rect = {int(i) for i in df.index[df["row"].isin(["a", "b"]) & df["col"].isin(["x", "y"])]}
    assert disclosed_cells(groups, rect) == []


def test_measures_blanked_only_in_suppressed_cells() -> None:
    values = {(r, c): safe(100 + i) for i, (r, c) in enumerate(itertools.product("abc", "xyz"))}
    values["b", "y"] = (4, 2, 0.9)
    df = cube(values)
    out = apply_suppression(df, ("row", "col"), "period", RULES)
    assert out.loc[out["suppressed"], ["contracts", "earned_premium"]].isna().all().all()
    shown = out[~out["suppressed"]]
    assert shown[["contracts", "earned_premium"]].notna().all().all()
    pd.testing.assert_series_equal(
        shown["contracts"].astype("int64"), df.loc[shown.index, "contracts"].astype("int64"), check_names=False
    )
    assert "dealers" not in out.columns and "max_dealer_share" not in out.columns


# --------------------------------------------------------------------------- cell totals from dealer contributions
def test_cell_totals_dealer_count_and_dominance() -> None:
    base = dict(period="2026-01", state="TX", coverage_tier="Gold", term_band="48", incurred_claims=0.0,
                claim_count=0, exposure_years=1.0)
    contrib = pd.DataFrame([
        {**base, "dealer_code": "D1", "contracts": 30, "earned_premium": 3000.0},
        {**base, "dealer_code": "D2", "contracts": 10, "earned_premium": 1000.0},
        {**base, "dealer_code": "D3", "contracts": 10, "earned_premium": 6000.0},  # premium-dominant
    ])
    cell = cell_totals(contrib).iloc[0]
    assert cell["contracts"] == 50 and cell["dealers"] == 3
    assert cell["max_dealer_share"] == pytest.approx(0.6)  # max of contract share (0.6) and premium share (0.6)
    assert cell["loss_ratio"] == 0.0


# --------------------------------------------------------------------------- against the real warehouse
needs_gold = pytest.mark.skipif(not WAREHOUSE.exists(), reason="warehouse not built (run python -m pipeline.run)")


@pytest.fixture(scope="module")
def published() -> dict[str, pd.DataFrame]:
    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        try:
            return build_benchmarks(con)
        except duckdb.CatalogException:
            pytest.skip("core marts not built yet")


@needs_gold
def test_published_tables_are_aggregate_only(published: dict[str, pd.DataFrame]) -> None:
    banned = {"contract_id", "vin", "vin_hash", "customer_key", "dealer_code", "dealer_key", "dealer_name"}
    for df in published.values():
        assert not banned & set(df.columns)
        shown = df[~df["suppressed"]]
        assert (shown["contracts"] >= RULES.min_contracts).all()


@needs_gold
def test_t12_benchmark_never_discloses(published: dict[str, pd.DataFrame]) -> None:
    assert_no_disclosure(published["peer_benchmark_t12"].rename(columns={"window": "period"}), DIMS)


@needs_gold
def test_recent_monthly_benchmarks_never_disclose(published: dict[str, pd.DataFrame]) -> None:
    monthly = published["peer_benchmark_monthly"].rename(columns={"month": "period"})
    recent = sorted(monthly["period"].unique())[-6:]
    assert_no_disclosure(monthly[monthly["period"].isin(recent)], DIMS)


@needs_gold
def test_national_total_ties_to_core_facts(published: dict[str, pd.DataFrame]) -> None:
    monthly = published["peer_benchmark_monthly"]
    total = monthly[(monthly[list(DIMS)] == ALL).all(axis=1) & ~monthly["suppressed"]].set_index("month")
    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        mart = con.execute(
            """
            select strftime(m.month_start, '%Y-%m') as month, sum(m.earned_premium) as ep, sum(m.incurred_claims) as ic
            from core.fct_contract_monthly m
            join core.dim_dealer d using (dealer_key)
            join core.dim_product p using (product_key)
            where d.dealer_key <> '-1' and d.state is not null and p.category_code = 'VSC' and m.exposure_years > 0
            group by 1
            """
        ).df().set_index("month")
    joined = total.join(mart, how="inner")
    assert len(joined) > 12
    assert np.allclose(joined["earned_premium"].astype(float), joined["ep"], atol=1.0)
    assert np.allclose(joined["incurred_claims"].astype(float), joined["ic"], atol=1.0)


# --------------------------------------------------------------------------- per-dealer packets
@pytest.fixture(scope="module")
def wh_con():
    with duckdb.connect(str(WAREHOUSE), read_only=True) as con:
        try:
            con.execute("select 1 from core.dim_user_dealer_access limit 1")
        except duckdb.CatalogException:
            pytest.skip("core marts not built yet")
        yield con


@needs_gold
def test_one_packet_per_reportable_dealer_and_no_demo(wh_con) -> None:
    ps = packets(wh_con)
    assert ps and len({(p.tenant_code, p.dealer_number) for p in ps}) == len(ps)
    demo = {r[0] for r in wh_con.execute(
        "select dealer_number from intermediate.int_reportable_dealers where not is_reportable").fetchall()}
    assert demo and not demo & {p.dealer_number for p in ps}


@needs_gold
def test_packet_holds_only_its_own_dealer_rows(wh_con) -> None:
    p = max(packets(wh_con), key=lambda x: x.dealer_number)
    tables = packet_tables(wh_con, p)
    pending = tables["pending_contracts"]["contract_number"].tolist()
    if pending:
        owners = wh_con.execute(
            "select distinct dealer_key, tenant_key from core.dim_contract where contract_number in "
            f"({', '.join('?' * len(pending))})", pending).fetchall()
        assert owners == [(p.dealer_key, p.tenant_key)]
    written = tables["production"]["contracts_written"].sum()
    # production is T12 from the certified scorecard mart (not lifetime facts): see tests/test_platform.py
    expected = wh_con.execute(
        "select coalesce(sum(contracts_written), 0) from dealer.mart_dealer_scorecard, core.dim_reporting_date r "
        "where dealer_key = ? and tenant_key = ? and month_start > r.last_complete_month - interval 12 month "
        "and month_start <= r.last_complete_month",
        [p.dealer_key, p.tenant_key]).fetchone()[0]
    assert written == expected and expected > 0


@needs_gold
def test_packet_with_another_tenant_is_empty(wh_con) -> None:
    p = packets(wh_con)[0]
    other = Packet(p.tenant_key + 100, p.tenant_code, p.reportable_name, p.primary_color, p.secondary_color,
                   p.dealer_key, p.dealer_number, p.dealer_name)
    tables = packet_tables(wh_con, other)
    assert all(len(df) == 0 or df.fillna(0).eq(0).all().all() for df in tables.values())


@needs_gold
def test_packets_are_written_with_tenant_branding(wh_con, tmp_path) -> None:
    written = build_packets(wh_con, tmp_path)
    first = written[0]
    assert {"production.csv", "pending_contracts.csv", "remittance_aging.csv", "claims_kpi.csv", "index.html"} <= {
        f.name for f in first.iterdir()}
    tenant = first.parent.name
    name = wh_con.execute("select reportable_name from core.dim_tenant where tenant_code = ?", [tenant]).fetchone()[0]
    assert name in (first / "index.html").read_text(encoding="utf-8")
