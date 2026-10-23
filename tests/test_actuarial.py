"""Actuarial math checked against hand-worked examples, a textbook triangle and a small synthetic book.

Unit tests need no warehouse. The last tests read the built warehouse (``WAREHOUSE_PATH``) read-only
and skip when it is absent or locked.
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from actuarial import (
    age_to_age_factors,
    bornhuetter_ferguson,
    build_triangle,
    cancellation_refund,
    chain_ladder,
    cumulative_factors,
    custom_fraction,
    earned_fraction,
    mileage_months,
    unearned_premium,
)
from actuarial import build as b
from actuarial.__main__ import compute, run
from actuarial.data import Inputs, load_inputs, missing_tables, scope_claims
from actuarial.earning import METHODS, validate_curve
from actuarial.emergence import early_claim_by_dealer, early_claim_emergence, first_claim_days
from actuarial.forecast import MODELS, backtest, forecast, score
from actuarial.methods import assign_methods, load_curves, load_rules

CURVES = ["pro_rata", "rule_of_78s", "reverse_rule_of_78s"]
ROOT = Path(__file__).resolve().parents[1]


# =========================================================================== earning curves
def test_hand_worked_12_month_contract_after_3_months() -> None:
    # n = 12, sum of digits 1..12 = 78.  Rule of 78s earns 12+11+10 = 33/78; reverse earns 1+2+3 = 6/78.
    assert earned_fraction(3, 12, "pro_rata") == pytest.approx(0.25)
    assert earned_fraction(3, 12, "rule_of_78s") == pytest.approx(33 / 78)
    assert earned_fraction(3, 12, "reverse_rule_of_78s") == pytest.approx(6 / 78)


@pytest.mark.parametrize("curve", CURVES)
def test_curves_start_at_zero_end_at_one_and_never_decrease(curve: str) -> None:
    k = np.arange(0, 61)
    e = earned_fraction(k, 60, curve)
    assert e[0] == 0 and e[-1] == pytest.approx(1.0)
    assert np.all(np.diff(e) > 0)
    assert earned_fraction(75, 60, curve) == pytest.approx(1.0)  # past expiry stays fully earned


def test_rule_of_78s_mirrors_reverse_rule() -> None:
    n, k = 36, np.arange(0, 37)
    np.testing.assert_allclose(
        earned_fraction(k, n, "rule_of_78s"), 1 - earned_fraction(n - k, n, "reverse_rule_of_78s")
    )


def test_front_loaded_curve_earns_most_early() -> None:
    e = {c: earned_fraction(12, 48, c) for c in CURVES}
    assert e["rule_of_78s"] > e["pro_rata"] > e["reverse_rule_of_78s"]


def test_mileage_limited_contract_earns_on_greater_of_time_and_miles() -> None:
    # 12 of 36 months (1/3 of time) but 30k of 45k miles (2/3 of mileage): mileage wins.
    assert earned_fraction(12, 36, miles_used=30_000, term_miles=45_000) == pytest.approx(2 / 3)
    assert earned_fraction(12, 36, miles_used=5_000, term_miles=45_000) == pytest.approx(1 / 3)
    assert earned_fraction(20, 36, miles_used=50_000, term_miles=45_000) == pytest.approx(1.0)
    with pytest.raises(ValueError):
        earned_fraction(12, 36, miles_used=30_000)


def test_mileage_months_from_a_monthly_rate() -> None:
    # 2,500 mi/month on a 36 mo / 45k contract: 12 months = 30k miles = 24 equivalent months.
    assert mileage_months(12, 36, 2_500, 45_000) == pytest.approx(24)
    assert mileage_months(12, 36, 500, 45_000) == pytest.approx(12)  # low mileage: time wins
    assert mileage_months(30, 36, 2_500, 45_000) == pytest.approx(36)  # cap reached: fully earned
    np.testing.assert_allclose(mileage_months([12, 12], 36, 2_500, [np.nan, 0]), [12, 12])  # no limit


def test_custom_curve_interpolates_its_knots() -> None:
    t, e = [0, 0.5, 1], [0, 0.2, 1]
    assert custom_fraction(6, 12, t, e) == pytest.approx(0.2)
    assert custom_fraction(3, 12, t, e) == pytest.approx(0.1)
    assert custom_fraction(9, 12, t, e) == pytest.approx(0.6)
    assert custom_fraction(20, 12, t, e) == pytest.approx(1.0)
    for bad in ([0, 0.5, 0.9], [0.1, 0.5, 1]):
        with pytest.raises(ValueError):
            validate_curve(bad, [0, 0.5, 1])
    with pytest.raises(ValueError):
        validate_curve([0, 0.5, 1], [0, 0.6, 0.5])  # decreasing


def test_repo_curve_files_are_valid_and_cover_every_custom_rule() -> None:
    curves = load_curves()
    rules = load_rules(curves=curves)
    assert set(rules.loc[rules["method"] == "custom", "curve_id"]) <= set(curves)
    # the new-vehicle VSC curve is back-loaded, the used one front-loaded, relative to pro-rata
    t_new, e_new = curves["vsc_new_warranty_overlap"]
    t_used, e_used = curves["vsc_used_early_wear"]
    assert np.all(e_new[1:-1] < t_new[1:-1]) and np.all(e_used[1:-1] > t_used[1:-1])


def test_bad_inputs_are_rejected() -> None:
    with pytest.raises(ValueError):
        earned_fraction(1, 12, "straight_line")
    with pytest.raises(ValueError):
        earned_fraction(1, 0)


def test_unearned_premium_reserve_for_a_small_book() -> None:
    premium = np.array([1200.0, 1200.0, 600.0])
    k, n = np.array([3, 12, 0]), np.array([12, 12, 24])
    np.testing.assert_allclose(unearned_premium(premium, k, n), [900.0, 0.0, 600.0])
    assert unearned_premium(1200.0, 3, 12, "rule_of_78s") == pytest.approx(1200 * 45 / 78)


def test_cancellation_refund_is_pro_rata_minus_fee() -> None:
    assert cancellation_refund(2400.0, 6, 24) == pytest.approx(1750.0)
    assert cancellation_refund(2400.0, 6, 24, fee=0) == pytest.approx(1800.0)
    assert cancellation_refund(2000.0, 365, 730) == pytest.approx(950.0)
    assert cancellation_refund(1000.0, 23.9, 24) == 0.0
    assert cancellation_refund(1200.0, 3, 12, curve="rule_of_78s") == pytest.approx(1200 * 45 / 78 - 50, abs=0.01)


# =========================================================================== method assignment
def test_most_specific_rule_wins(tmp_path: Path) -> None:
    (tmp_path / "m.csv").write_text(
        "category_code,subcategory_code,new_used,method,curve_id,rationale\n"
        "*,*,*,pro_rata,,default\n"
        "VSC,*,*,rule_of_78s,,category\n"
        "VSC,*,NEW,reverse_rule_of_78s,,category+new\n"
        "VSC,VSC-PT,*,pro_rata_mileage,,subcategory\n",
        encoding="utf-8",
    )
    rules = load_rules(tmp_path / "m.csv", curves={})
    c = pd.DataFrame({
        "category_code": ["VSC", "VSC", "VSC", "GAP", None],
        "subcategory_code": ["VSC-SC", "VSC-SC", "VSC-PT", "GAP", None],
        "new_used": ["USED", "NEW", "NEW", "NEW", None],
    })
    got = assign_methods(c, rules)["method"].tolist()
    assert got == ["rule_of_78s", "reverse_rule_of_78s", "pro_rata_mileage", "pro_rata", "pro_rata"]


def test_rules_are_validated(tmp_path: Path) -> None:
    head = "category_code,subcategory_code,new_used,method,curve_id,rationale\n"
    for body in ("*,*,*,straight_line,,x\n", "*,*,*,pro_rata,,x\nVSC,*,*,custom,nope,x\n", "VSC,*,*,pro_rata,,x\n"):
        (tmp_path / "m.csv").write_text(head + body, encoding="utf-8")
        with pytest.raises(ValueError):
            load_rules(tmp_path / "m.csv", curves={})


# =========================================================================== triangles & chain-ladder
HAND = pd.DataFrame(
    {1: [100.0, 110.0, 120.0], 2: [150.0, 165.0, np.nan], 3: [165.0, np.nan, np.nan]},
    index=pd.Index(["A", "B", "C"], name="cohort"),
)


def test_build_triangle_cumulates_and_masks_the_future() -> None:
    rows = pd.DataFrame({
        "cohort": ["A", "A", "A", "A", "B", "B", "C"],
        "age": [1, 1, 2, 3, 1, 2, 1],
        "value": [60, 40, 50, 15, 110, 55, 120],
    })
    tri = build_triangle(rows, {"A": 3, "B": 2, "C": 1, "D": 1})
    pd.testing.assert_frame_equal(
        tri.loc[["A", "B", "C"]], HAND, check_names=False, check_dtype=False, check_index_type=False
    )
    assert tri.loc["D", 1] == 0.0 and np.isnan(tri.loc["D", 2])
    with pytest.raises(ValueError, match="beyond"):
        build_triangle(rows, {"A": 3, "B": 1, "C": 1})


def test_chain_ladder_hand_worked() -> None:
    cl = chain_ladder(HAND)
    np.testing.assert_allclose(cl.ata.to_numpy(), [1.5, 1.1])
    np.testing.assert_allclose(cl.cdf.to_numpy(), [1.65, 1.1, 1.0])
    np.testing.assert_allclose(cl.summary["ultimate"].to_numpy(), [165.0, 181.5, 198.0])
    np.testing.assert_allclose(cl.summary["ibnr"].to_numpy(), [0.0, 16.5, 78.0])
    assert cl.total_ibnr == pytest.approx(94.5)


def test_tail_factor_applies_to_every_cohort() -> None:
    cl = chain_ladder(HAND, tail=1.05)
    np.testing.assert_allclose(cl.summary["ultimate"].to_numpy(), np.array([165.0, 181.5, 198.0]) * 1.05)


def test_bornhuetter_ferguson_hand_worked() -> None:
    bf = bornhuetter_ferguson(pd.Series([120.0]), pd.Series([1.65]), pd.Series([300.0]), 0.6)
    assert bf["pct_unreported"].iloc[0] == pytest.approx(0.393939, abs=1e-6)
    assert bf["ibnr"].iloc[0] == pytest.approx(70.909, abs=1e-3)
    assert bf["ultimate"].iloc[0] == pytest.approx(190.909, abs=1e-3)


# Taylor & Ashe (1983) cumulative paid triangle, the one used in Mack (1993) and most textbooks.
TAYLOR_ASHE = [
    [357848, 1124788, 1735330, 2218270, 2745596, 3319994, 3466336, 3606286, 3833515, 3901463],
    [352118, 1236139, 2170033, 3353322, 3799067, 4120063, 4647867, 4914039, 5339085],
    [290507, 1292306, 2218525, 3235179, 3985995, 4132918, 4628910, 4909315],
    [310608, 1418858, 2195047, 3757447, 4029929, 4381982, 4588268],
    [443160, 1136350, 2128333, 2897821, 3402672, 3873311],
    [396132, 1333217, 2180715, 2985752, 3691712],
    [440832, 1288463, 2419861, 3483130],
    [359480, 1421128, 2864498],
    [376686, 1363294],
    [344014],
]


def test_chain_ladder_reproduces_the_textbook_triangle() -> None:
    tri = pd.DataFrame([r + [np.nan] * (10 - len(r)) for r in TAYLOR_ASHE], columns=range(1, 11), dtype=float)
    cl = chain_ladder(tri)
    published = [3.4906, 1.7473, 1.4574, 1.1739, 1.1038, 1.0863, 1.0539, 1.0766, 1.0177]
    np.testing.assert_allclose(cl.ata.to_numpy(), published, atol=5e-5)
    assert cl.total_ibnr == pytest.approx(18_680_856, abs=5)
    assert cl.summary["ibnr"].iloc[-1] == pytest.approx(4_625_811, abs=1)
    np.testing.assert_allclose(cumulative_factors(age_to_age_factors(tri)).to_numpy(), cl.cdf.to_numpy())


def test_factors_can_skip_development_from_before_a_process_change() -> None:
    # cohorts (quarter indexes) 0..3; links that start before period 2 ran slow (age 1 -> 2 doubled),
    # links from period 2 on add 10%. Cohort 0's age 2 -> 3 link starts in period 1: excluded too.
    tri = pd.DataFrame({1: [100.0, 100.0, 100.0, 100.0], 2: [200.0, 200.0, 110.0, np.nan],
                        3: [220.0, 210.0, np.nan, np.nan]}, index=pd.Index([0, 1, 2, 3], name="cohort"))
    assert age_to_age_factors(tri)[1] == pytest.approx(510 / 300)  # all history, blended
    f = age_to_age_factors(tri, from_period=2)
    assert f[1] == pytest.approx(1.10)  # only cohort 2 (its age-1 cell is period 2)
    assert f[2] == pytest.approx(210 / 200)  # cohort 1's age 2 -> 3 link starts in period 2; cohort 0's in 1
    assert chain_ladder(tri, from_period=2).summary.loc[3, "ultimate"] == pytest.approx(100 * 1.10 * 1.05)
    from actuarial.reserving import link_cohort_counts
    assert list(link_cohort_counts(tri, from_period=2)) == [1, 1]
    assert age_to_age_factors(tri, from_period=2, min_cohorts=2)[1] == pytest.approx(510 / 300)  # falls back
    assert age_to_age_factors(tri, from_period=99)[1] == pytest.approx(510 / 300)  # nothing qualifies


def test_factor_start_is_the_newest_systems_first_quarter() -> None:
    c = pd.DataFrame({"source_system": ["legacy", "legacy", "core", "core"],
                      "effective_date": pd.to_datetime(["2019-02-01", "2022-12-31", "2023-01-05", "2024-06-01"])})
    s = b.Settings(as_of=date(2026, 9, 30))
    assert b.factor_start(c, s) == 2023 * 4
    assert b.factor_start(c.assign(source_system="core"), s) is None  # one system: no regime change
    assert b.factor_start(c.drop(columns="source_system"), s) is None
    assert b.factor_start(c, b.Settings(as_of=s.as_of, factor_start_quarter="2024Q3")) == 2024 * 4 + 2
    assert b.factor_start(c, b.Settings(as_of=s.as_of, factor_start_quarter=None)) is None


def test_young_cohort_premium_allows_for_cancellations_still_to_come() -> None:
    # Old cohorts (2022Q1..Q3): 10 x 1,200 each, 12 months; 3 cancel 6 months in (month 7: keep 7/12).
    # The young cohort (2022Q4, age 1) has no cancellation yet but should expect the same.
    rows = []
    for q, start in enumerate(["2022-01-01", "2022-04-01", "2022-07-01", "2022-10-01"]):
        eff = pd.Timestamp(start)
        for i in range(10):
            cancel = eff + pd.DateOffset(months=6) if (i < 3 and q < 3) else pd.NaT
            rows.append((f"c{q}{i}", eff, cancel))
    c = pd.DataFrame(rows, columns=["contract_id", "effective_date", "cancel_date"]).assign(
        premium=1200.0, term_months=12)
    val_q = 2022 * 4 + 3
    p = b.premium_development(c.assign(cancel_date=c["cancel_date"].where(c["cancel_date"] <= "2022-12-31")),
                              val_q)
    keep = (7 * 1200 * 3 / 12 + 7 * 1200) / 12000  # 3 keep 7/12, 7 keep all
    old = p.loc[2022 * 4]
    assert old["premium_to_date"] == pytest.approx(12000 * keep) and old["retention_cdf"] == pytest.approx(1.0)
    young = p.loc[val_q]
    assert young["written_premium"] == young["premium_to_date"] == pytest.approx(12000)
    assert young["ultimate_premium"] == pytest.approx(12000 * keep)  # not the full gross
    assert (p["ultimate_premium"] <= p["written_premium"] * keep + 1e-6).all()
    p2 = b.premium_development(c, 2022 * 4 + 2)  # 2022Q3's cancels land in 2023Q1, after this valuation
    assert p2.loc[2022 * 4 + 2, "premium_to_date"] == pytest.approx(12000)


def test_valuation_uses_last_complete_quarter() -> None:
    assert b.valuation_quarter(date(2026, 10, 22)) == 2026 * 4 + 2  # 2026Q3
    assert b.valuation_quarter(date(2026, 9, 30)) == 2026 * 4 + 2
    assert b.valuation_quarter(date(2026, 9, 29)) == 2026 * 4 + 1


# =========================================================================== earned premium engine
def _tiny() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """c1 in force 12 mo VSC used; c2 cancels in month 3; c3 is a high-mileage 12 mo / 12k contract."""
    contracts = pd.DataFrame({
        "contract_id": ["c1", "c2", "c3"],
        "dealer_number": ["D1", "D1", "D2"],
        "category_code": ["VSC", "VSC", "SPECIALTY"],
        "subcategory_code": ["VSC-PT", "VSC-PT", "RV"],
        "product_code": ["VSC-PT-12", "VSC-PT-12", "RV-12"],
        "new_used": ["USED", "USED", "NEW"],
        "term_months": [12, 12, 12],
        "term_miles": [12_000.0, 12_000.0, 12_000.0],
        "claim_wait_days": [30, 30, 30],
        "effective_date": pd.to_datetime(["2024-01-15", "2024-01-20", "2024-01-05"]),
        "cancel_date": pd.to_datetime([pd.NaT, "2024-03-10", pd.NaT]),
        "premium": [1200.0, 1200.0, 1200.0],
        "odometer": [50_000.0, 40_000.0, 10_000.0],
        "cancel_odometer": [np.nan, np.nan, np.nan],
    })
    claims = pd.DataFrame({
        "claim_id": ["k1", "k2", "k3"],
        "contract_id": ["c1", "c1", "c3"],
        "loss_date": pd.to_datetime(["2024-02-01", "2024-08-01", "2024-04-01"]),
        "reported_date": pd.to_datetime(["2024-02-03", "2024-08-01", "2024-04-02"]),
        "odometer_at_loss": [51_000.0, np.nan, 16_000.0],  # c3 drives ~2,000 mi/month
        "status_group": ["paid", "open", "paid"],
    })
    lines = pd.DataFrame({
        "claim_id": ["k1", "k1", "k2", "k3"],
        "contract_id": ["c1", "c1", "c1", "c3"],
        "posted_date": pd.to_datetime(["2024-02-03", "2024-04-02", "2024-08-01", "2024-04-02"]),
        "paid": [0.0, 300.0, 0.0, 100.0],
        "incurred": [250.0, 50.0, 500.0, 100.0],
    })
    return contracts, claims, lines


def _rules() -> pd.DataFrame:
    return load_rules()


def test_earned_premium_ties_out_under_every_method() -> None:
    contracts, claims, lines = _tiny()
    s = b.Settings(as_of=date(2025, 6, 30))
    c = b.prepare_contracts(contracts, claims, _rules(), s)
    ep = b.earned_premium(c, b.claim_amounts(claims, lines, s.as_of), s, load_curves())
    assert set(ep["method"]) == set(METHODS)
    assert set(c["method"]) == {"custom", "pro_rata_mileage"}  # used VSC custom curve, RV mileage-aware
    k = 3  # c2: effective Jan, cancelled Mar -> earns months 1..3, rest refunded
    for method, g in ep.groupby("method"):
        assert g["written_premium"].sum() == pytest.approx(3600)
        assert g["incurred_losses"].sum() == pytest.approx(900)
        if method in CURVES:
            assert g["earned_premium"].sum() == pytest.approx(2400 + 1200 * float(earned_fraction(k, 12, method)))
    sel = ep[ep["method"] == "selected"]
    t, e = load_curves()["vsc_used_early_wear"]
    expected = 1200 + 1200 * float(custom_fraction(3, 12, t, e)) + 1200  # c1 full, c2 custom 3/12, c3 full
    assert sel["earned_premium"].sum() == pytest.approx(expected)
    assert set(ep["posting_period_key"]) >= {202401, 202412}
    assert ep["month"].max() == pd.Timestamp("2024-12-01")


def test_mileage_aware_earning_finishes_a_high_mileage_contract_early() -> None:
    contracts, claims, lines = _tiny()
    s = b.Settings(as_of=date(2025, 6, 30))
    c = b.prepare_contracts(contracts, claims, _rules(), s)
    rate = c.set_index("contract_id")["monthly_miles"]
    months = (pd.Timestamp("2024-04-01") - pd.Timestamp("2024-01-05")).days / 30.4375
    assert rate["c3"] == pytest.approx(6000 / months)
    assert rate["c2"] == pytest.approx(rate["c1"])  # no reading: segment (USED) median
    ep = b.earned_premium(c, b.claim_amounts(claims, lines, s.as_of), s, load_curves())
    rv = ep[(ep["product_code"] == "RV-12") & (ep["method"] == "pro_rata_mileage")].set_index("month")
    months_to_cap = int(np.ceil(12_000 / rate["c3"]))
    assert rv["earned_premium"].sum() == pytest.approx(1200)
    assert rv.loc[rv.index[months_to_cap - 1], "unearned_premium"] == pytest.approx(0)
    assert rv["contracts_in_force"].iloc[months_to_cap - 1:].sum() == 0  # expired on miles before 12 months


# =========================================================================== reserving on payment lines
def test_policy_triangles_and_reserve_split_tie_out() -> None:
    contracts, claims, lines = _tiny()
    s = b.Settings(as_of=date(2024, 12, 31))
    val_q = b.valuation_quarter(s.as_of)
    c = b.prepare_contracts(contracts, claims, _rules(), s)
    tri = b.policy_triangles(c, claims, lines, val_q)
    assert set(tri) == {"ALL", "VSC", "SPECIALTY"}
    rep = tri["ALL"]["reported"]
    assert rep.loc[2024 * 4, 1] == pytest.approx(250)  # Q1 posting
    assert rep.loc[2024 * 4, 2] == pytest.approx(400)  # + 50 (k1) + 100 (k3) in Q2
    assert rep.loc[2024 * 4, 4] == pytest.approx(900)
    assert tri["ALL"]["paid"].loc[2024 * 4, 4] == pytest.approx(400)
    assert tri["ALL"]["count"].loc[2024 * 4, 4] == 3
    assert tri["VSC"]["reported"].loc[2024 * 4, 4] + tri["SPECIALTY"]["reported"].loc[2024 * 4, 4] == 900

    ep = b.earned_premium(c, b.claim_amounts(claims, lines, s.as_of), s, load_curves())
    acc, _ = b.accident_quarters(claims, lines, ep, s)
    occ = b.occurred_ibnr(c, claims, lines, acc, val_q)
    ibnr, factors = b.ibnr_by_cohort(c, tri, s, occ)
    row = ibnr[ibnr["segment"] == "ALL"].iloc[0]
    assert row["reported_to_date"] == pytest.approx(900) and row["paid_to_date"] == pytest.approx(400)
    assert row["ultimate_premium"] == pytest.approx(1200 + 300 + 1200)
    assert row["selected_ibnr"] + row["unexpired_risk_losses"] == pytest.approx(row["development_to_ultimate"])
    assert set(factors["basis"]) == {"reported", "paid"}
    assert {"written_premium", "premium_to_date", "premium_retention_cdf"} <= set(ibnr.columns)
    # BF while less than half reported (1 / CDF < 0.5); ALL is the sum of the segments
    seg = ibnr[ibnr["segment"] != "ALL"]
    assert ((seg["method"] == "bornhuetter_ferguson") == (1 / seg["cdf"] < s.bf_max_reported)).all()
    assert (ibnr.loc[ibnr["segment"] == "ALL", "method"] == "sum_of_segments").all()
    src = b.ibnr_by_source(claims, lines.assign(source_system="core"), acc, val_q)
    assert sum(src.values()) == pytest.approx(acc["ibnr"].sum())


def test_occurred_ibnr_allocates_the_accident_quarter_ibnr() -> None:
    # Accident quarters A, B; A develops 100 -> 150 (factor 1.5); B is at age 1 with 80 -> IBNR 40.
    contracts = pd.DataFrame({
        "contract_id": ["c1", "c2"], "category_code": ["VSC", "GAP"],
        "effective_date": pd.to_datetime(["2024-01-01", "2024-01-01"]),
    })
    claims = pd.DataFrame({"claim_id": ["a", "b"], "loss_date": pd.to_datetime(["2024-04-01", "2024-07-01"])})
    lines = pd.DataFrame({
        "claim_id": ["a", "a", "b"], "contract_id": ["c1", "c1", "c2"],
        "posted_date": pd.to_datetime(["2024-04-02", "2024-07-02", "2024-07-02"]),
        "incurred": [100.0, 50.0, 80.0], "paid": [0.0, 0.0, 0.0],
    })
    val_q = 2024 * 4 + 2
    latest = {2024 * 4 + 1: 2, 2024 * 4 + 2: 1}
    ln = lines.assign(cohort=[2024 * 4 + 1, 2024 * 4 + 1, 2024 * 4 + 2], age=[1, 2, 1], value=lines["incurred"])
    cl = chain_ladder(build_triangle(ln, latest))
    acc = pd.DataFrame({"quarter_start": pd.to_datetime(["2024-04-01", "2024-07-01"]),
                        "cdf": cl.summary["cdf"].to_numpy()})
    occ = b.occurred_ibnr(contracts, claims, lines, acc, val_q)
    assert occ["ALL"].sum() == pytest.approx(cl.total_ibnr) == pytest.approx(40)
    assert occ["GAP"].sum() == pytest.approx(40) and occ["VSC"].sum() == pytest.approx(0)


# =========================================================================== one cut-off, PDR and the IBNR back-test
def _tiny_after_valuation() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, b.Settings]:
    """The tiny book valued at 2024-09-30 with an as-of of 2024-10-15 and two post-valuation lines."""
    contracts, claims, lines = _tiny()
    claims = pd.concat([claims, pd.DataFrame({
        "claim_id": ["k4"], "contract_id": ["c1"], "loss_date": pd.to_datetime(["2024-09-20"]),
        "reported_date": pd.to_datetime(["2024-10-03"]), "odometer_at_loss": [np.nan], "status_group": ["open"],
    })], ignore_index=True)
    lines = pd.concat([lines, pd.DataFrame({
        "claim_id": ["k2", "k4"], "contract_id": ["c1", "c1"],
        "posted_date": pd.to_datetime(["2024-10-05", "2024-10-03"]), "paid": [0.0, 0.0], "incurred": [70.0, 40.0],
    })], ignore_index=True)
    return contracts, claims, lines, b.Settings(as_of=date(2024, 10, 15))


def test_valuation_view_cuts_premium_and_claims_at_the_valuation_date() -> None:
    contracts, claims, lines, s = _tiny_after_valuation()
    c = b.prepare_contracts(contracts, claims, _rules(), s)
    ep = b.earned_premium(c, b.claim_amounts(claims, lines, s.as_of), s, load_curves())
    assert ep["posting_period_key"].max() == 202410  # the monthly table runs to the as-of month
    vv = b.valuation_view(c, ep, claims, lines, s)
    pr = vv[(vv["segment"] == "ALL") & (vv["method"] == "pro_rata")].iloc[0]
    assert pr["valuation_period_key"] == 202409 and pr["valuation_date"] == pd.Timestamp("2024-09-30")
    assert pr["earned_premium"] == pytest.approx(900 + 300 + 900)  # c1, c2 (cancelled in month 3), c3: 9 months
    assert pr["unearned_premium"] == pytest.approx(300 + 0 + 300)
    assert pr["incurred_losses"] == pytest.approx(900)  # the October lines (70 + 40) are after the cut
    assert pr["loss_ratio"] == pytest.approx(900 / 2100)
    assert ep.loc[ep["method"] == "pro_rata", "earned_premium"].sum() == pytest.approx(2300)  # October earned whole
    seg = vv[vv["method"] == "pro_rata"].set_index("segment")
    assert seg.loc[["VSC", "SPECIALTY"], "earned_premium"].sum() == pytest.approx(pr["earned_premium"])
    assert set(vv["method"]) == set(METHODS)


def test_ibnr_runoff_scores_only_new_claims_and_reports_case_development_apart() -> None:
    _, claims, lines, s = _tiny_after_valuation()
    r = b.ibnr_runoff(claims, lines, s)
    assert r["ibnr_runoff_window_days"] == 15
    assert "ibnr_runoff_emerged" not in r  # new claims and known-claim development are never netted
    assert r["ibnr_runoff_new_claims"] == pytest.approx(40)  # k4: loss 9/20, first reported 10/3
    assert r["case_development_known_claims"] == pytest.approx(70)
    assert r["case_development_known_denied"] + r["case_development_known_other"] == pytest.approx(70)
    lag = b.report_lag_stats(claims, s.as_of)
    assert lag["report_lag_mean_days"] == pytest.approx((2 + 0 + 1 + 13) / 4)
    assert lag["report_lag_share_within_14d"] == 1.0 and lag["report_lag_share_30d_plus"] == 0.0
    # a case reserve released on a later denial does not offset new-claim emergence
    k2_after = lines["claim_id"].eq("k2") & (lines["posted_date"] > pd.Timestamp("2024-10-01"))
    released = lines.assign(incurred=np.where(k2_after, -500.0, lines["incurred"]))
    denied = claims.assign(status_group=np.where(claims["claim_id"] == "k2", "denied", claims["status_group"]))
    r2 = b.ibnr_runoff(denied, released, s, ibnr=50.0)
    assert r2["ibnr_runoff_new_claims"] == pytest.approx(40) and r2["ibnr_runoff_ratio"] == pytest.approx(0.8)
    assert r2["case_development_known_denied"] == pytest.approx(-500)
    assert 0 < r2["ibnr_runoff_expected_share"] <= 1
    assert r2["ibnr_runoff_ae_to_date"] == pytest.approx(40 / (50 * r2["ibnr_runoff_expected_share"]))
    assert r2["ibnr_runoff_flag"] == ("ibnr_short" if r2["ibnr_runoff_ae_to_date"] > 1 else "ok")


def test_runoff_expected_share_is_the_part_of_the_lag_a_window_can_see() -> None:
    val_end = pd.Timestamp("2024-09-30")
    loss = pd.to_datetime(["2023-01-10"] * 4)
    lag = [0, 10, 30, 80]  # E[L] = 30; E[min(L, 21)] = (0 + 10 + 21 + 21) / 4 = 13
    claims = pd.DataFrame({"loss_date": loss, "reported_date": loss + pd.to_timedelta(lag, unit="D")})
    assert b.runoff_expected_share(claims, val_end, 21) == pytest.approx(13 / 30)
    assert b.runoff_expected_share(claims, val_end, 365) == pytest.approx(1.0)
    # an IBNR that already shows more than the window should bring is flagged short
    s = b.Settings(as_of=date(2024, 10, 21))
    cl = pd.concat([claims.assign(claim_id=[f"o{i}" for i in range(4)], status_group="paid"), pd.DataFrame({
        "claim_id": ["n"], "loss_date": pd.to_datetime(["2024-09-25"]),
        "reported_date": pd.to_datetime(["2024-10-05"]), "status_group": ["open"]})], ignore_index=True)
    ln = pd.DataFrame({"claim_id": ["n"], "contract_id": ["c"], "posted_date": pd.to_datetime(["2024-10-05"]),
                       "paid": [0.0], "incurred": [90.0]})
    r = b.ibnr_runoff(cl, ln, s, ibnr=100.0)
    assert r["ibnr_runoff_ratio"] == pytest.approx(0.9)  # 90% of IBNR emerged; the window should see ~43%
    assert r["ibnr_runoff_ae_to_date"] > 1 and r["ibnr_runoff_flag"] == "ibnr_short"


def test_claims_scope_matches_the_claims_mart_rule() -> None:
    contracts, claims, lines = _tiny()
    claims = pd.concat([claims, pd.DataFrame({
        "claim_id": ["pre", "orphan"], "contract_id": ["c1", None],
        "loss_date": pd.to_datetime(["2023-12-01", "2024-03-01"]),  # pre: loss before the 2024-01-15 sale
        "reported_date": pd.to_datetime(["2026-01-01", "2024-03-02"]), "odometer_at_loss": [np.nan, np.nan],
        "status_group": ["paid", "paid"]})], ignore_index=True)
    lines = pd.concat([lines, pd.DataFrame({
        "claim_id": ["pre", "orphan", None], "contract_id": ["c1", None, None],
        "posted_date": pd.to_datetime(["2024-02-01", "2024-03-02", "2024-03-05"]),
        "paid": [0.0, 0.0, 0.0], "incurred": [77.0, 33.0, 5.0]})], ignore_index=True)
    out = scope_claims(Inputs(contracts, claims, lines))
    assert set(out.claims["claim_id"]) == {"k1", "k2", "k3"}
    assert dict(zip(out.excluded_claims["claim_id"], out.excluded_claims["reason"], strict=True)) == {
        "pre": "loss_before_sale", "orphan": "no_contract"}
    ex = out.excluded_lines.groupby("reason")["incurred"].sum()
    assert ex["loss_before_sale"] == pytest.approx(77) and ex["no_contract"] == pytest.approx(38)
    assert out.lines["incurred"].sum() + out.excluded_lines["incurred"].sum() == pytest.approx(lines["incurred"].sum())
    # the excluded claim never reaches a triangle or an accident quarter
    s = b.Settings(as_of=date(2024, 12, 31))
    c = b.prepare_contracts(out.contracts, out.claims, _rules(), s)
    tri = b.policy_triangles(c, out.claims, out.lines, b.valuation_quarter(s.as_of))
    assert tri["ALL"]["reported"].ffill(axis=1).iloc[:, -1].sum() == pytest.approx(900)


def _young_segment_book() -> tuple[pd.DataFrame, dict[str, dict[str, pd.DataFrame]], b.Settings]:
    """OLD: 8 cohorts of 6-month terms (fully observed); NEW: 2 cohorts of 36-month terms (history 6 months)."""
    rows = []
    for q in range(8):
        rows.append(("OLD", pd.Timestamp(2023, 1 + 3 * (q % 4), 1) + pd.DateOffset(years=q // 4), 6))
    for q in range(6, 8):
        rows.append(("NEW", pd.Timestamp(2023, 1 + 3 * (q % 4), 1) + pd.DateOffset(years=q // 4), 36))
    c = pd.DataFrame({
        "contract_id": [f"c{i}" for i in range(len(rows))], "category_code": [r[0] for r in rows],
        "effective_date": [r[1] for r in rows], "term_months": [r[2] for r in rows],
        "premium": 1000.0, "cancel_date": pd.NaT,
    })
    s = b.Settings(as_of=date(2024, 12, 31))
    val_q = b.valuation_quarter(s.as_of)
    q0 = 2023 * 4
    tris = {}
    for seg, cohorts in (("OLD", range(q0, q0 + 8)), ("NEW", range(q0 + 6, q0 + 8))):
        latest = {q: val_q - q + 1 for q in cohorts}
        inc = pd.DataFrame([(q, a, 100.0) for q in cohorts for a in (1, 2) if a <= latest[q]],
                           columns=["cohort", "age", "value"])
        tri = build_triangle(inc, latest)
        tris[seg] = {"reported": tri, "paid": tri * 0.5, "count": tri / 100}
    allc = sorted(set(range(q0, q0 + 8)))
    tris["ALL"] = {k: tris["OLD"][k].add(tris["NEW"][k].reindex(index=allc, columns=tris["OLD"][k].columns),
                                         fill_value=0).where(tris["OLD"][k].notna()) for k in ("reported", "paid",
                                                                                                "count")}
    return c, tris, s


def test_a_segment_younger_than_its_terms_gets_a_tail_above_one() -> None:
    c, tris, s = _young_segment_book()
    tails = b.segment_tails(c, tris, s)
    assert tails["OLD"]["source"] == "observed" and tails["OLD"]["reported"] == 1.0
    new = tails["NEW"]
    assert new["max_term"] > new["observed_months"]
    assert new["reported"] > 1 and new["paid"] > 1 and new["source"] != "observed"
    # its oldest cohort is 6 of 36 months in: about 6x still to run on exposure alone
    assert new["reported"] == pytest.approx(b.exposure_tail(c[c["category_code"] == "NEW"], 2), rel=1e-9)
    ibnr, factors = b.ibnr_by_cohort(c, tris, s)
    f = factors[(factors["segment"] == "NEW") & (factors["basis"] == "reported")]
    assert f["cdf"].iloc[-1] == pytest.approx(new["reported"]) and (f["tail_source"] != "observed").all()
    new_rows = ibnr[ibnr["segment"] == "NEW"]
    assert (new_rows["cdf"] > 1).all()  # no cohort of a young long-term segment counts as fully developed
    assert (new_rows["term_elapsed_share"] < s.elr_min_term_elapsed).all()
    assert new_rows["elr_source"].iloc[0] != "mature_cohorts"


def test_all_is_the_sum_of_the_segments() -> None:
    c, tris, s = _young_segment_book()
    ibnr, factors = b.ibnr_by_cohort(c, tris, s)
    total = ibnr[ibnr["segment"] == "ALL"].set_index("cohort_label")
    segs = ibnr[ibnr["segment"] != "ALL"].groupby("cohort_label")
    for col in ("selected_ultimate", "selected_ibnr", "unexpired_risk_losses", "reported_to_date",
                "ultimate_premium", "bf_ultimate", "cl_ultimate"):
        np.testing.assert_allclose(total[col], segs[col].sum().reindex(total.index), rtol=1e-9, err_msg=col)
    assert total["all_triangle_ultimate"].notna().all()  # the pooled triangle, kept as a diagnostic
    assert set(factors.loc[factors["segment"] == "ALL", "tail_source"]) == {"pooled_diagnostic"}


def test_premium_deficiency_is_tested_for_every_segment() -> None:
    vv = pd.DataFrame({"segment": ["ALL", "VSC", "GAP", "ALL"], "method": ["selected", "selected", "selected",
                                                                           "pro_rata"],
                       "unearned_premium": [1100.0, 1000.0, 100.0, 5.0]})
    ibnr = pd.DataFrame({"segment": ["ALL", "VSC", "GAP", "GAP"], "unexpired_risk_losses": [700.0, 600.0, 50.0, 50.0]})
    pdr = b.premium_deficiency(vv, ibnr, b.Settings(as_of=date(2024, 12, 31), maintenance_expense_ratio=0.1))
    assert list(pdr["segment"]) == ["GAP", "VSC", "ALL"]
    p = pdr.set_index("segment")
    assert p["unexpired_to_upr"].notna().all()
    assert p.loc["VSC", "unexpired_to_upr"] == pytest.approx(0.6) and not p.loc["VSC", "is_deficient"]
    assert p.loc["GAP", "cost_to_upr"] == pytest.approx(1.1)  # (50 + 50) x 1.1 / 100
    assert p.loc["GAP", "is_deficient"] and p.loc["GAP", "premium_deficiency_reserve"] == pytest.approx(10)
    assert p.loc["ALL", "unearned_premium"] == pytest.approx(1100)  # the selected method only


# =========================================================================== earning curve vs loss emergence
def _fit_book(n: int = 400) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Two 12-month families, one claim per contract at a known share of term.

    LIMITED_WARRANTY (pro-rata rule): losses uniform over the term; every second contract cancels at 50%
    of term, keeps only its losses before then, and has one more loss dated after the cancel (must drop).
    ANCILLARY/KEY (pro-rata rule): losses back-loaded, share of term t = sqrt(u), so CDF(t) = t^2.
    One extra LIMITED_WARRANTY contract is still in force at ``as_of`` and must be left out.
    """
    eff = pd.Timestamp("2020-01-01")
    year = pd.Timedelta(days=12 * 30.4375)
    u = (np.arange(n) + 0.5) / n
    rows, claims = [], []
    for fam, sub, times in (("LIMITED_WARRANTY", "LTW", u), ("ANCILLARY", "KEY", np.sqrt(u))):
        for i, t in enumerate(times):
            cid = f"{sub}{i}"
            cancel = fam == "LIMITED_WARRANTY" and i % 2 == 1
            if cancel and t > 0.5:
                claims.append((cid, eff + 0.75 * year, 999.0))  # after the cancel date: dropped
            elif not cancel or t <= 0.5:
                claims.append((cid, eff + t * year, 100.0))
            rows.append((cid, fam, sub, eff, eff + 0.5 * year if cancel else pd.NaT))
    rows.append(("LIVE", "LIMITED_WARRANTY", "LTW", pd.Timestamp("2022-03-01"), pd.NaT))
    claims.append(("LIVE", pd.Timestamp("2022-03-10"), 5_000.0))
    c = pd.DataFrame(rows, columns=["contract_id", "category_code", "subcategory_code", "effective_date",
                                    "cancel_date"])
    c = c.assign(dealer_number="D1", product_code=c["subcategory_code"] + "-12", new_used="NEW", term_months=12,
                 term_miles=np.nan, claim_wait_days=0, premium=1000.0, odometer=10_000.0, cancel_odometer=np.nan)
    k = pd.DataFrame(claims, columns=["contract_id", "loss_date", "incurred"])
    k["claim_id"] = [f"k{i}" for i in range(len(k))]
    cl = k.assign(reported_date=k["loss_date"], odometer_at_loss=np.nan, status_group="paid")
    lines = k.assign(posted_date=k["loss_date"], paid=k["incurred"])[
        ["claim_id", "contract_id", "posted_date", "paid", "incurred"]]
    return c, cl.drop(columns="incurred"), lines


def _fit(n: int = 400) -> pd.DataFrame:
    contracts, claims, lines = _fit_book(n)
    s = b.Settings(as_of=date(2022, 6, 30))
    c = b.prepare_contracts(contracts, claims, _rules(), s)
    return b.earning_fit(c, b.claim_amounts(claims, lines, s.as_of), s, load_curves())


def test_earning_fit_passes_a_curve_that_mirrors_emergence_despite_cancellations() -> None:
    fit = _fit()
    lw = fit[(fit["category_code"] == "LIMITED_WARRANTY") & (fit["level"] == "cell")]
    assert len(lw) == b.FIT_BANDS and list(lw["term_decile"]) == list(range(1, 11))
    assert lw["contracts"].iloc[0] == 400  # the in-force contract is not in the base
    assert lw["claims"].iloc[0] == 300  # 200 full-term + 100 before cancel; the 100 post-cancel losses dropped
    assert lw["incurred_losses"].sum() == pytest.approx(300 * 100.0)
    # life-table emergence: cancellations at 50% do not pull emergence early (a naive share would be 67%)
    assert lw.loc[lw["term_pct"] == 0.5, "emergence_cum_share"].iloc[0] == pytest.approx(0.5, abs=0.02)
    np.testing.assert_allclose(lw["curve_cum_share"], np.arange(1, 11) / 10)  # pro-rata selected
    assert lw["max_abs_gap"].iloc[0] < 0.03 and lw["status"].iloc[0] == "pass"
    np.testing.assert_allclose(lw["ae_ratio"], 1.0, atol=0.1)  # losses track earned premium in every band
    assert lw["emergence_cum_share"].iloc[-1] == pytest.approx(1.0)


def test_earning_fit_flags_a_curve_that_earns_ahead_of_losses() -> None:
    fit = _fit()
    key = fit[(fit["category_code"] == "ANCILLARY") & (fit["level"] == "cell")].set_index("term_decile")
    assert key.loc[5, "emergence_cum_share"] == pytest.approx(0.25, abs=0.01)  # CDF t^2 at mid-term
    assert key.loc[5, "gap"] == pytest.approx(0.25, abs=0.01)  # pro-rata earns 50%: +25 points ahead
    assert key["max_abs_gap"].iloc[0] == pytest.approx(0.25, abs=0.01)
    assert key["worst_decile"].iloc[0] == 5 and key["status"].iloc[0] == "warn"
    assert key["noise_band"].iloc[0] == pytest.approx(1.36 / np.sqrt(400))
    assert key["best_method"].iloc[0] == "reverse_rule_of_78s" and key["best_method_gap"].iloc[0] < 0.05
    assert key.loc[1, "ae_ratio"] < 0.2 and key.loc[10, "ae_ratio"] > 1.5  # losses lag, then outrun earning
    fam = fit[(fit["level"] == "family") & (fit["category_code"] == "ANCILLARY")]
    assert set(fam["subcategory_code"]) == {"*"} and fam["status"].iloc[0] == "warn"

    summ = b.earning_fit_summary(fit)
    assert summ["earning_fit_gate"] == "warn" and summ["earning_fit_rows_warn"] == 2  # ANCILLARY cell + family
    assert summ["earning_fit_worst_row"].startswith("ANCILLARY/") and "decile 5" in summ["earning_fit_worst_row"]
    assert summ["earning_fit_worst_gap"] == pytest.approx(0.25, abs=0.01)
    ok = b.earning_fit_summary(fit[fit["category_code"] == "LIMITED_WARRANTY"])
    assert ok["earning_fit_gate"] == "pass" and ok["earning_fit_rows_warn"] == 0


def test_earning_fit_does_not_gate_thin_or_noisy_rows() -> None:
    fit = _fit(n=12)  # 12 claims a cell: below FIT_MIN_CLAIMS
    assert set(fit["status"]) == {"low_credibility"}
    assert b.earning_fit_summary(fit)["earning_fit_gate"] == "pass"
    # a 25-point gap on 25 claims is still inside the 95% KS noise band (1.36 / sqrt(25) = 27%)
    fit = _fit(n=25)
    key = fit[(fit["category_code"] == "ANCILLARY") & (fit["level"] == "cell")]
    assert key["status"].iloc[0] == "within_noise"


# =========================================================================== early-claim emergence
def test_early_claims_count_only_exposed_contracts() -> None:
    contracts = pd.DataFrame({
        "contract_id": ["a", "b", "c", "d"],
        "dealer_number": ["D1", "D1", "D2", "D2"],
        "category_code": "VSC", "subcategory_code": "VSC-PT", "new_used": "USED",
        "claim_wait_days": [30, 30, 30, 30],
        "effective_date": pd.to_datetime(["2024-01-01", "2024-01-01", "2024-01-01", "2024-06-01"]),
    })
    claims = pd.DataFrame({
        "contract_id": ["a", "b", "b", "d"],
        "loss_date": pd.to_datetime(["2024-01-21", "2024-03-15", "2023-12-01", "2024-06-10"]),
        "status_group": ["paid", "denied", "paid", "paid"],
    })
    as_of = date(2024, 7, 1)
    days = first_claim_days(contracts, claims, as_of, include_denied=True)
    assert days["a"] == 20 and days["b"] == 74 and np.isnan(days["c"])  # pre-sale loss ignored
    em = early_claim_emergence(contracts, claims, as_of, windows=(30, 90)).groupby("window_days").sum(
        numeric_only=True)
    assert em.loc[30, "contracts_exposed"] == 4 and em.loc[90, "contracts_exposed"] == 3  # d is 30 days old
    assert em.loc[30, "early_claim_contracts"] == 2  # a (20 days) and d (9 days)
    assert em.loc[30, "within_wait_period"] == 2
    assert em.loc[90, "early_claim_contracts"] == 1  # d not exposed; b's only early claim was denied
    assert em.loc[90, "early_reported_contracts"] == 2
    dl = early_claim_by_dealer(contracts, claims, as_of, windows=(90,), min_exposed=1).set_index("dealer_number")
    assert dl.loc["D1", "early_claim_contracts"] == 2 and dl.loc["D2", "early_claim_contracts"] == 0
    assert dl["expected_early_claims"].sum() == pytest.approx(dl["early_claim_contracts"].sum())


# =========================================================================== forecast & backtest
def test_models_on_hand_series() -> None:
    y = np.array([0.4, 0.5, 0.6, 0.7])
    np.testing.assert_allclose(MODELS["naive"](y, 2), [0.7, 0.7])
    np.testing.assert_allclose(MODELS["mean_4q"](y, 1), [0.55])
    np.testing.assert_allclose(MODELS["trend_8q"](y, 2), [0.8, 0.9])
    np.testing.assert_allclose(MODELS["ewma"](np.array([1.0, 0.0]), 1), [0.7])


def test_backtest_never_peeks_at_the_future() -> None:
    rng = np.random.default_rng(1)
    y = 0.5 + rng.normal(0, 0.03, 20)
    bt = backtest(y, horizon=4, origins=8)
    y2 = y.copy()
    y2[-1] = 9.0  # change only the last point
    bt2 = backtest(y2, horizon=4, origins=8)
    early = bt["origin"] + bt["step"] - 1 < len(y) - 1  # forecasts that target an earlier point
    np.testing.assert_allclose(bt.loc[early, "forecast"], bt2.loc[early, "forecast"])
    assert bt["origin"].min() >= 8 and (bt["origin"] + bt["step"] <= len(y)).all()


def test_band_never_narrows_with_the_horizon() -> None:
    # a series whose last points are calm: later steps have fewer, smaller errors (raw RMSE shrinks)
    y = pd.Series([0.5, 0.6, 0.4, 0.55, 0.45, 0.5, 0.62, 0.38, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5],
                  index=np.arange(14))
    fc, sc = forecast(y)
    fut = fc[fc["kind"] == "forecast"]
    width = (fut["upper"] - fut["lower"]).to_numpy()
    assert (np.diff(width) >= -1e-12).all()
    assert (fut["band_rmse"] >= fut["step_rmse"].fillna(0) - 1e-12).all()
    assert fut["step_rmse"].iloc[-1] < fut["step_rmse"].iloc[0]  # the raw per-step RMSE did shrink
    assert list(fut["band_points"]) == [6, 5, 4, 3]  # 6 origins from min_train = 8 on 14 points
    assert set(sc["points_by_step"]) == {"6/5/4/3"}


def test_trend_wins_on_a_trending_series_and_band_is_reported() -> None:
    y = pd.Series(0.40 + 0.01 * np.arange(16), index=np.arange(2020 * 4, 2020 * 4 + 16))
    fc, sc = forecast(y)
    assert sc.loc[sc["is_selected"], "model"].item() == "trend_8q"
    assert sc.set_index("model").loc["trend_8q", "mae"] == pytest.approx(0, abs=1e-12)
    fut = fc[fc["kind"] == "forecast"]
    np.testing.assert_allclose(fut["forecast"], 0.40 + 0.01 * np.arange(16, 20))
    assert (fut["lower"] <= fut["forecast"]).all() and (fut["upper"] >= fut["forecast"]).all()
    assert len(fc[fc["kind"] == "history"]) == 16
    assert score(pd.DataFrame(columns=["model", "error"])).empty


# =========================================================================== end to end on a tiny warehouse
def _core(con: duckdb.DuckDBPyConnection, n: int = 600, seed: int = 7) -> None:
    """A small synthetic ``core`` star schema with the columns the actuarial readers use."""
    rng = np.random.default_rng(seed)
    eff = pd.Timestamp("2022-01-01") + pd.to_timedelta(rng.integers(0, 1000, n), unit="D")
    cat = rng.choice(["VSC", "GAP"], n, p=[0.8, 0.2])
    sub = np.where(cat == "VSC", rng.choice(["VSC-PT", "VSC-EX"], n), "GAP")
    cancelled = rng.random(n) < 0.15
    cancel = eff + pd.to_timedelta(rng.integers(30, 400, n), unit="D")
    keys = [f"k{i}" for i in range(n)]
    con.execute("create schema core")
    con.register("c", pd.DataFrame({
        "contract_key": keys, "contract_number": [f"VSC-{i}" for i in range(n)], "source_system": "admin_core",
        "dealer_number": rng.choice([f"D{i}" for i in range(8)], n), "category_code": cat,
        "subcategory_code": sub, "effective_date": eff.date, "is_cancelled": cancelled,
        "cancel_date": np.where(cancelled, cancel.date, None),
    }))
    con.execute("create table core.dim_contract as select * from c")
    con.register("t", pd.DataFrame({
        "contract_key": keys, "term_months": rng.choice([24, 36, 48], n),
        "term_miles": np.where(cat == "VSC", 36_000, np.nan), "claim_wait_days": 30,
    }))
    con.execute("create table core.dim_contract_terms as select * from t")
    con.register("f", pd.DataFrame({"contract_key": keys, "reserve_amount": rng.uniform(500, 1500, n).round(2)}))
    con.execute("create table core.dim_contract_finance as select * from f")
    con.register("v", pd.DataFrame({"contract_key": keys, "new_used": rng.choice(["New", "Used"], n),
                                    "odometer": rng.integers(0, 80_000, n)}))
    con.execute("create table core.dim_contract_vehicle as select * from v")
    con.execute("""create table core.fct_cancellation as select contract_key, 0 as odometer,
                   true as is_effective_cancel from core.dim_contract where false""")
    has = rng.random(n) < 0.35
    ck = [k for k, h in zip(keys, has, strict=True) if h]
    loss = eff[has] + pd.to_timedelta(rng.integers(5, 600, has.sum()), unit="D")
    con.register("k", pd.DataFrame({
        "claim_key": [f"cl{i}" for i in range(len(ck))], "contract_key": ck, "loss_date": loss.date,
        "reported_date": (loss + pd.Timedelta(days=3)).date, "odometer_at_loss": np.nan,
        "status_group": rng.choice(["paid", "denied"], len(ck), p=[0.9, 0.1]),
    }))
    con.execute("create table core.dim_claim as select * from k")
    con.execute("""create table core.fct_claim_payment_line as
        select claim_key, contract_key, cast(reported_date as timestamp) as posted_at,
               0.0 as paid_amount, 600.0 as incurred_change from core.dim_claim
        union all
        select claim_key, contract_key, cast(reported_date as timestamp) + interval 40 day,
               600.0, 0.0 from core.dim_claim where status_group = 'paid'
        union all
        select null, '-1', timestamp '2023-05-01', 10.0, 10.0""")


def test_run_writes_actuarial_schema_compat_views_and_report_page(tmp_path: Path) -> None:
    wh = tmp_path / "wh.duckdb"
    with duckdb.connect(str(wh)) as con:
        assert missing_tables(con)
        with pytest.raises(RuntimeError, match="dbt build"):
            load_inputs(con)
        _core(con)
    counts = run(wh, as_of=date(2025, 3, 31))
    assert counts["earned_premium"] > 0 and counts["loss_ratio_forecast"] > 4
    with duckdb.connect(str(wh)) as con:
        for t in ("earned_premium_by_curve", "loss_triangle", "ibnr_by_cohort", "development_factors"):
            assert con.execute(f"select count(*) from gold_actuarial.{t}").fetchone()[0] > 0, t
        sql = "select distinct curve from gold_actuarial.earned_premium_by_curve"
        curves = {r[0] for r in con.execute(sql).fetchall()}
        assert curves == set(CURVES)
        s = dict(con.execute("select metric, value from actuarial.run_summary").fetchall())
        assert float(s["incurred_in_policy_triangle"]) + float(s["incurred_lines_without_contract"]) + float(
            s["incurred_excluded_loss_before_sale"]) == pytest.approx(float(s["incurred_lines_to_valuation"]))
        assert float(s["incurred_lines_without_contract"]) == pytest.approx(10.0)
        assert float(s["selected_ultimate_all"]) == pytest.approx(float(s["selected_ultimate_sum_of_segments"]))
        for k in ("upr_selected", "unexpired_to_upr_ratio", "ibnr_runoff_new_claims", "ibnr_runoff_ratio",
                  "ibnr_runoff_ae_to_date", "ibnr_runoff_flag", "case_development_known_claims",
                  "report_lag_mean_days", "ibnr_days_of_incurred", "earned_itd_selected_at_valuation",
                  "earning_fit_gate", "earning_fit_worst_gap", "earning_fit_worst_row", "earning_fit_rows_warn"):
            assert k in s, k
        assert s["valuation_period_key"] == "202503"
        assert s["earning_fit_gate"] in ("pass", "warn")
        fit = con.execute("select * from actuarial.earning_fit").df()
        assert set(fit["level"]) == {"cell", "family"} and (fit.groupby(
            ["category_code", "subcategory_code", "new_used", "level"]).size() == b.FIT_BANDS).all()
        ib = con.execute("select * from actuarial.ibnr_by_cohort").df()
        dev = ib["development_to_ultimate"] >= 0  # Reported + IBNR + unexpired = Ultimate (the page's columns)
        np.testing.assert_allclose((ib["reported_to_date"] + ib["selected_ibnr"] + ib["unexpired_risk_losses"])[dev],
                                   ib.loc[dev, "selected_ultimate"], rtol=1e-9)
        pdr = con.execute("select * from actuarial.premium_deficiency").df()
        assert set(pdr["segment"]) == set(ib["segment"]) and pdr["unexpired_to_upr"].notna().all()
        from reports.actuarial import build as plugin_build
        pages = plugin_build(con, tmp_path / "site")
    assert [p.path for p in pages] == ["actuarial.html"] and len(pages[0].sections) >= 6
    titles = [sec.title for sec in pages[0].sections]
    assert any("back-test" in t for t in titles) and any("premium-deficiency" in t for t in titles)
    assert {"Earning vs claim emergence: VSC", "Earning vs claim emergence: GAP"} <= set(titles)  # one per family
    assert any(t.startswith("Earning-fit gate") for t in titles)
    labels = {k.label: k.sub for k in pages[0].kpis}
    assert "Earned claims reserve, ITD" in labels and "Earned premium, ITD" not in labels  # say which basis
    assert any("unearned claims reserve (UPR, claims slice)" in sub for sub in labels.values())
    assert (tmp_path / "site" / "downloads" / "actuarial" / "earned_premium.csv").is_file()
    run(wh, as_of=date(2025, 3, 31))  # re-runnable: views and tables are replaced in place


def test_strict_fit_turns_the_earning_fit_warning_into_a_failure(tmp_path: Path, monkeypatch) -> None:
    from actuarial import __main__ as cli

    wh = tmp_path / "wh.duckdb"
    with duckdb.connect(str(wh)) as con:
        _core(con)
    warn = {"earning_fit_rows_credible": 1, "earning_fit_rows_warn": 1, "earning_fit_worst_gap": 0.3,
            "earning_fit_worst_row": "VSC/VSC-PT/NEW (custom, decile 5)", "earning_fit_gap_threshold": 0.1,
            "earning_fit_gate": "warn"}
    monkeypatch.setattr(b, "earning_fit_summary", lambda fit: warn)
    assert run(wh, as_of=date(2025, 3, 31))["earning_fit"] > 0  # default: warn and carry on
    with pytest.raises(cli.EarningFitError, match="VSC/VSC-PT/NEW"):
        run(wh, as_of=date(2025, 3, 31), strict_fit=True)
    assert cli.main(["--warehouse", str(wh), "--as-of", "2025-03-31", "--strict-fit"]) == 1


def test_doc_figures_come_from_run_summary(tmp_path: Path) -> None:
    from actuarial.docs import DOC, END, START, figures_block, write_figures

    text = DOC.read_text(encoding="utf-8")
    assert text.count(START) == 1 and text.count(END) == 1  # the repo doc keeps one generated block
    doc = tmp_path / "A.md"
    doc.write_text(f"intro\n{START}\nold $81K\n{END}\noutro\n", encoding="utf-8")
    summ = {"valuation_date": "2026-09-30", "ibnr_selected_all": "43240.9", "reported_all": "12649960",
            "ibnr_runoff_ratio": "0.41", "forecast_next_quarter": "0.486", "forecast_model": "mean_4q"}
    assert write_figures(summ, {"lower": 0.43, "upper": 0.54}, doc)
    out = doc.read_text(encoding="utf-8")
    assert out.startswith("intro\n") and out.endswith("outro\n") and "$81K" not in out
    assert "$43.2K" in out and "48.6%" in out and "43.0% to 54.0%" in out and "mean 4q" in out
    assert figures_block(summ) in out.replace("\n| Next-quarter band (about 95%) | 43.0% to 54.0% |", "")
    bare = tmp_path / "B.md"
    bare.write_text("no markers", encoding="utf-8")
    assert not write_figures(summ, None, bare) and bare.read_text(encoding="utf-8") == "no markers"


# =========================================================================== the built warehouse (optional)
WAREHOUSE = Path(os.environ.get("WAREHOUSE_PATH", ROOT / "data" / "warehouse" / "warehouse.duckdb"))


@pytest.fixture(scope="module")
def warehouse_tables() -> dict[str, pd.DataFrame]:
    if not WAREHOUSE.is_file():
        pytest.skip("no built warehouse")
    try:
        con = duckdb.connect(str(WAREHOUSE), read_only=True)
    except duckdb.Error as exc:  # locked by a writer
        pytest.skip(f"warehouse not readable: {exc}")
    with con:
        if missing_tables(con):
            pytest.skip("core marts not built")
        inputs = load_inputs(con)
        flags = con.execute("""select c.contract_key, c.effective_date, s.is_early_claim_90
                               from core.dim_contract_claim_summary s
                               join core.dim_contract c using (contract_key)""").df()
    as_of = max(inputs.contracts["effective_date"].max(), inputs.lines["posted_date"].max()).date()
    tables = compute(inputs, b.Settings(as_of=as_of))
    tables["_core_flags"] = flags.assign(as_of=pd.Timestamp(as_of))
    return tables


def test_warehouse_control_totals(warehouse_tables: dict[str, pd.DataFrame]) -> None:
    s = warehouse_tables["run_summary"].set_index("metric")["value"].astype(str)
    total = float(s["incurred_lines_to_valuation"])
    assert float(s["incurred_in_policy_triangle"]) + float(s["incurred_lines_without_contract"]) + float(
        s["incurred_excluded_loss_before_sale"]) == pytest.approx(total)
    # true IBNR is the accident-quarter IBNR, allocated; it is small next to reported losses
    assert float(s["ibnr_selected_all"]) == pytest.approx(float(s["ibnr_accident_quarter_basis"]), rel=0.05, abs=5_000)
    assert float(s["ibnr_selected_all"]) < 0.2 * float(s["reported_all"])
    ep = warehouse_tables["earned_premium"]
    tot = ep.groupby("method")[["incurred_losses", "earned_premium"]].sum()
    lr = tot["incurred_losses"] / tot["earned_premium"]
    assert lr["rule_of_78s"] < lr["pro_rata"] < lr["reverse_rule_of_78s"]
    assert warehouse_tables["forecast_backtest"]["is_selected"].sum() == 1


def test_warehouse_early_claims_match_the_core_flags(warehouse_tables: dict[str, pd.DataFrame]) -> None:
    f = warehouse_tables["_core_flags"]
    exposed = pd.to_datetime(f["effective_date"]) + pd.Timedelta(days=90) <= f["as_of"]
    core_90 = int(f.loc[exposed, "is_early_claim_90"].sum())
    em = warehouse_tables["early_claim_emergence"]
    ours = int(em.loc[em["window_days"] == 90, "early_claim_contracts"].sum())
    # same rule (first non-denied loss within 90 days); core also counts a few pre-effective losses
    assert abs(ours - core_90) <= max(5, 0.02 * core_90)


def test_warehouse_ibnr_runoff_and_upr_are_reported(warehouse_tables: dict[str, pd.DataFrame]) -> None:
    s = warehouse_tables["run_summary"].set_index("metric")["value"].astype(str)
    assert float(s["case_development_known_claims"]) == pytest.approx(
        float(s["case_development_known_denied"]) + float(s["case_development_known_other"]))
    ratio = float(s["ibnr_runoff_new_claims"]) / float(s["ibnr_selected_all"])
    assert float(s["ibnr_runoff_ratio"]) == pytest.approx(ratio)
    assert s["ibnr_runoff_flag"] == ("ibnr_short" if float(s["ibnr_runoff_ae_to_date"]) > 1 else "ok")
    assert float(s["upr_selected"]) > float(s["unexpired_risk_losses_all"])  # no deficiency in total
    vv = warehouse_tables["valuation_view"]
    assert (vv["valuation_date"] == pd.Timestamp(s["valuation_date"])).all()


def test_warehouse_ibnr_against_generator_truth(warehouse_tables: dict[str, pd.DataFrame]) -> None:
    """Admin Core IBNR vs the generator's EXPECTED Admin Core IBNR at valuation (+-25%).

    Why expected, not realized: one valuation's realized IBNR is a few dozen late-reported claims
    with skewed severities. On the current warehouse 64 claims were unreported at valuation (about 72
    expected) but their paid ones averaged about $1,470 against about $950, so the realized $66K
    is far above what the generator's process implies, and a calibrated model would miss a +-25% band
    on it most of the time. The model never sees the generator; only this test reads it.

    Expected IBNR = unreported part + known-claim part, both on Admin Core:
    - unreported: claims per loss day (plan claims with a loss in the 91 days to valuation, reported or
      not) x sum over k of P(report lag > k days) x mean planned ultimate per claim (losses in the year to
      valuation, denied claims as 0). P comes from the generator's lag model (``generator.sim``): a
      geometric prompt lag and a uniform late tail.
    - known claims, net of the expected case-reserve release: per planned claim reported by valuation with
      a payment or supplement still to come, its planned ultimate less the incurred posted by valuation.
      Those claims already exist; their gap is what their reserves will release (or add) and is not the
      noisy part.
    The estimate is ``ibnr_source_admin_core``. The realized truth is printed as information only.
    """
    import sqlite3

    from generator import sim

    db = ROOT / "data" / "source" / "admin_core.db"
    if not db.is_file():
        pytest.skip("no generator source database")
    s = warehouse_tables["run_summary"].set_index("metric")["value"].astype(str)
    if "ibnr_source_admin_core" not in s:
        pytest.skip("no Admin Core lines in the warehouse")
    val = s["valuation_date"]
    with sqlite3.connect(db) as con:
        try:
            plan = pd.read_sql(
                "select claim_number, loss_day, reported_day, coalesce(pp_day, '') as pp_day, "
                "coalesce(supp_day, '') as supp_day, paid_total + coalesce(supp_amount, 0) as ultimate "
                "from _gen_claim_plan where loss_day <= ?", con, params=[val])
        except (sqlite3.Error, pd.errors.DatabaseError):
            pytest.skip("no generator claim plan")
    vkey = int(val.replace("-", ""))
    with duckdb.connect(str(WAREHOUSE), read_only=True) as wh:
        posted = wh.execute("""
            select d.claim_number, sum(l.incurred_change) as incurred
            from core.fct_claim_payment_line l join core.dim_claim d using (claim_key)
            where l.source_system = 'admin_core' and l.posted_date_key <= ? group by 1""", [vkey]).df()
    plan["claim_number"] = plan["claim_number"].astype(str)
    posted["claim_number"] = posted["claim_number"].astype(str)
    x = plan.merge(posted, on="claim_number", how="left").fillna({"incurred": 0.0})
    pending = (x["reported_day"] > val) | (x["pp_day"] > val) | (x["supp_day"] > val)
    realized = float((x.loc[pending, "ultimate"] - x.loc[pending, "incurred"]).sum())

    age = (pd.Timestamp(val) - pd.to_datetime(x["loss_day"])).dt.days
    per_day = float((age < 91).sum()) / 91
    severity = float(x.loc[age < 365, "ultimate"].mean())
    lo, hi = sim.LATE_REPORT_DAYS  # integers in [lo, hi)
    k = np.arange(hi + 1)
    p_unreported = ((1 - sim.LATE_REPORT_SHARE) * (1 - sim.PROMPT_REPORT_P) ** (k + 1)
                    + sim.LATE_REPORT_SHARE * np.clip((hi - 1 - k) / (hi - lo), 0, 1))
    unreported = per_day * float(p_unreported.sum()) * severity
    known = pending & (x["reported_day"] <= val)
    known_dev = float((x.loc[known, "ultimate"] - x.loc[known, "incurred"]).sum())
    expected = unreported + known_dev
    if expected <= 0:
        pytest.skip("no expected IBNR at valuation in the generator plan")
    est = float(s["ibnr_source_admin_core"])
    err = est / expected - 1
    print(f"Admin Core IBNR {est:,.0f} vs expected {expected:,.0f} (unreported {unreported:,.0f}, known claims "
          f"{known_dev:,.0f}): signed error {err:+.1%}; realized {realized:,.0f} (information only)")
    assert abs(err) <= 0.25, f"IBNR {est:,.0f} vs expected {expected:,.0f} ({err:+.1%})"
    total = float(s["ibnr_accident_quarter_basis"])
    by_source = sum(float(v) for k, v in s.items() if k.startswith("ibnr_source_"))
    assert by_source == pytest.approx(total, rel=1e-9, abs=1e-6)


IBNR_BACKTEST_MAX_SCALED_MAE = 0.60


def test_warehouse_ibnr_backtest_over_12_valuations(warehouse_tables: dict[str, pd.DataFrame]) -> None:
    """The IBNR method re-run at the 12 quarter-ends before valuation, scored against what emerged after.

    Bound: mean absolute error <= ``IBNR_BACKTEST_MAX_SCALED_MAE`` (60%) of the mean realized IBNR.
    One quarter's realized IBNR on this book varies by about half its mean from quarter to quarter
    (a few dozen late claims with skewed severities), so even a perfect forecast of the mean scores a
    mean absolute error of about 0.8 x that, roughly 40-45% of the mean. 60% allows that plus a modest
    bias; a method that ignored the report lag or doubled the factors would land well above it.
    """
    bt = warehouse_tables["ibnr_backtest"]
    s = warehouse_tables["run_summary"].set_index("metric")["value"].astype(str)
    assert len(bt) == b.IBNR_BACKTEST_VALUATIONS
    assert np.allclose(bt["ibnr_estimate"], bt["ibnr_unreported"] + bt["ibnr_known_claims"])
    # every run sees only data up to its own quarter-end, all before the current valuation
    assert (bt["valuation_date"] < pd.Timestamp(s["valuation_date"])).all()
    mean_real = float(bt["realized"].mean())
    mae = float(bt["error"].abs().mean())
    noise = float(bt["realized"].std()) / mean_real
    print(f"IBNR back-test: MAE {mae:,.0f} on mean realized {mean_real:,.0f} ({mae / mean_real:.0%}); "
          f"realized varies {noise:.0%} of its mean; "
          f"estimate / realized {bt['ibnr_estimate'].sum() / bt['realized'].sum():.2f}")
    assert float(s["ibnr_backtest_scaled_mae"]) == pytest.approx(mae / mean_real)
    assert mae <= IBNR_BACKTEST_MAX_SCALED_MAE * mean_real, f"MAE {mae / mean_real:.0%} of mean realized"


def test_warehouse_reserving_basis(warehouse_tables: dict[str, pd.DataFrame]) -> None:
    """Factors from the current posting regime; BF premium net of expected cancellations."""
    s = warehouse_tables["run_summary"].set_index("metric")["value"].astype(str)
    ib = warehouse_tables["ibnr_by_cohort"]
    a = ib[ib["segment"] == "VSC"]
    # no cohort is credited with more premium than it has kept so far, or than older cohorts keep
    assert (a["ultimate_premium"] <= a["premium_to_date"] + 1e-6).all()
    older = a[a["age_quarters"] >= 8]
    bound = (older["premium_to_date"] / older["written_premium"]).max()
    bf = a[a["method"] == "bornhuetter_ferguson"]
    assert len(bf) and (bf["ultimate_premium"] <= bf["written_premium"] * bound + 1e-6).all()
    assert ((a["method"] == "bornhuetter_ferguson") == (1 / a["cdf"] < 0.5)).all()
    mature = a[a["term_elapsed_share"] >= b.Settings(as_of=date.today()).elr_min_term_elapsed]
    elr = mature["cl_ultimate"].sum() / mature["ultimate_premium"].sum()
    assert a["bf_expected_loss_ratio"].iloc[0] == pytest.approx(elr) and a["elr_source"].iloc[0] == "mature_cohorts"
    # ALL is the sum of the segments (the pooled triangle is a diagnostic only)
    segs, total = ib[ib["segment"] != b.ALL], ib[ib["segment"] == b.ALL]
    for col in ("selected_ultimate", "selected_ibnr", "unexpired_risk_losses", "reported_to_date"):
        assert total[col].sum() == pytest.approx(segs[col].sum(), rel=1e-9), col
    # a segment younger than its longest term develops past its oldest age
    f = warehouse_tables["development_factors"]
    head = f[(f["basis"] == "reported") & (f["segment"] != b.ALL)].drop_duplicates("segment").set_index("segment")
    young = head[head["max_term_months"] > head["observed_months"]]
    assert len(young) and (young["tail"] > 1).all() and (young["tail_source"] != "observed").all()
    if s["factor_start_quarter"] != "all":  # two systems: the age-1 factor is fitted after the migration
        assert float(s["accident_link_age1"]) < 1.08
    fut = warehouse_tables["loss_ratio_forecast"]
    fut = fut[fut["kind"] == "forecast"]
    assert (np.diff((fut["upper"] - fut["lower"]).to_numpy()) >= -1e-12).all()


def test_warehouse_earning_fit_is_measured_and_gated(warehouse_tables: dict[str, pd.DataFrame]) -> None:
    fit = warehouse_tables["earning_fit"]
    s = warehouse_tables["run_summary"].set_index("metric")["value"].astype(str)
    head = fit.drop_duplicates(["category_code", "subcategory_code", "new_used", "level"])
    assert s["earning_fit_gate"] == ("warn" if (head["status"] == "warn").any() else "pass")
    assert int(s["earning_fit_rows_warn"]) == int((head["status"] == "warn").sum())
    np.testing.assert_allclose(fit.loc[fit["term_decile"] == b.FIT_BANDS, "curve_cum_share"], 1.0)
    vsc = head[(head["category_code"] == "VSC") & (head["level"] == "family")]
    assert len(vsc) == 2 and (vsc["max_abs_gap"] < b.FIT_GAP_WARN).all()  # the fitted VSC curves track emergence
