"""Report plug-in: the actuarial page of the static site, built from schema ``actuarial`` only.

Follows the shell's plug-in contract: ``build(con, out_dir) -> list[reports.build.Page]``. The shell
renders the pages and adds them to the nav. The CSV downloads go to ``<out_dir>/downloads/actuarial/``.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

NAV_LABEL = "Actuarial"
DOWNLOADS = ("earned_premium", "ibnr_by_cohort", "accident_quarter", "loss_ratio_forecast",
             "forecast_backtest", "early_claim_emergence", "early_claim_dealer", "valuation_view",
             "premium_deficiency", "earning_fit")
METHOD_LABELS = {
    "selected": "Selected (by product)",
    "pro_rata": "Pro-rata",
    "pro_rata_mileage": "Pro-rata, mileage-aware",
    "rule_of_78s": "Rule of 78s",
    "reverse_rule_of_78s": "Reverse Rule of 78s",
}


def _df(con: duckdb.DuckDBPyConnection, sql: str) -> pd.DataFrame:
    return con.execute(sql).df()


def finance_earned_reserve(con: duckdb.DuckDBPyConnection, period_key: int) -> float | None:
    """Finance's earned claims reserve to ``period_key`` (core.fct_earned_disbursement), if it is built."""
    try:
        v = con.execute("select sum(earned_reserve) from core.fct_earned_disbursement where posting_period_key <= ?",
                        [period_key]).fetchone()[0]
    except duckdb.Error:
        return None
    return float(v) if v is not None else None


def finance_all_buckets(con: duckdb.DuckDBPyConnection, period_key: int) -> tuple[float, float] | None:
    """Finance's all-bucket earned premium to ``period_key`` and net written minus earned (the all-bucket
    UPR), from ``finance.mart_pnl_by_product``: the basis the Overview, P&L and Finance pages call
    'Earned premium'. None when the finance mart is not built."""
    try:
        e, w = con.execute("select sum(earned_premium), sum(net_written_coverage_cost) "
                           "from finance.mart_pnl_by_product where posting_period_key <= ?", [period_key]).fetchone()
    except duckdb.Error:
        return None
    return (float(e), float(w) - float(e)) if e is not None and w is not None else None


STATUS_TEXT = {"warn": ("critical", "curve misses"), "within_noise": ("warning", "within noise"),
               "pass": ("good", "fits"), "low_credibility": ("", "too few claims")}


def _status(v: str) -> dict:
    cls, text = STATUS_TEXT.get(v, ("", v))
    return {"status": cls, "text": text} if cls else {"text": text}


def fit_sections(fit: pd.DataFrame, summ: dict) -> list:
    """One chart per product family (premium earned vs claims emerged by term decile), then the gate table."""
    from reports import charts
    from reports.build import Section, chart_section, num, pct

    if fit.empty:
        return []
    fam = fit[fit["level"] == "family"]
    out = []
    for cat in sorted(fam["category_code"].unique()):
        f = fam[fam["category_code"] == cat]
        nus = sorted(f["new_used"].unique())
        wide = f.pivot_table(index="term_pct", columns="new_used",
                             values=["emergence_cum_share", "curve_cum_share", "ae_ratio"]).sort_index()
        df = pd.DataFrame({"term_pct": [0.0, *wide.index]})
        ys = {}
        for nu in nus:
            df[f"e_{nu}"] = [0.0, *wide[("curve_cum_share", nu)]]
            df[f"x_{nu}"] = [0.0, *wide[("emergence_cum_share", nu)]]
            ys[f"e_{nu}"] = f"{nu.title()}: premium earned (selected)"
            ys[f"x_{nu}"] = f"{nu.title()}: claims emerged (actual)"
        head = f.drop_duplicates("new_used").set_index("new_used")
        verdict = "; ".join(
            f"{nu.title()} {head.at[nu, 'method'].replace('_', ' ')}: max gap {pct(head.at[nu, 'max_abs_gap'])} "
            f"(noise {pct(head.at[nu, 'noise_band'])}, {num(head.at[nu, 'claims'])} claims, "
            f"{STATUS_TEXT.get(head.at[nu, 'status'], ('', ''))[1]}"
            + (f"; {head.at[nu, 'best_method'].replace('_', ' ')} fits best at {pct(head.at[nu, 'best_method_gap'])}"
               if head.at[nu, "best_method"] not in ("selected", head.at[nu, "method"]) else "") + ")"
            for nu in nus)
        out.append(chart_section(
            f"Earning vs claim emergence: {cat if len(cat) <= 3 else cat.replace('_', ' ').title()}",
            "Share of lifetime premium earned vs share of lifetime claims that had happened, by share of term "
            f"elapsed, on expired contracts (cancelled ones count up to their cancel date). {verdict}.",
            charts.lines(df, "term_pct", ys, yfmt=".0%", hover=".1%", height=260),
            f"act-fit-{cat.lower()}",
            ["Term elapsed", *[f"{nu.title()} {k}" for nu in nus for k in ("earned", "emerged", "A/E")]],
            [[pct(t, 0), *[v for nu in nus for v in (pct(wide.at[t, ("curve_cum_share", nu)]),
                                                     pct(wide.at[t, ("emergence_cum_share", nu)]),
                                                     num(wide.at[t, ("ae_ratio", nu)], 2))]]
             for t in wide.index],
        ))
    head = fit.drop_duplicates(["category_code", "subcategory_code", "new_used", "level"])
    head = head.assign(_c=head["status"].eq("low_credibility")).sort_values(["_c", "max_abs_gap"],
                                                                            ascending=[True, False])
    gate = summ.get("earning_fit_gate", "")
    thr = float(summ.get("earning_fit_gap_threshold", "nan"))
    out.append(Section(
        "Earning-fit gate: does each curve mirror loss emergence?",
        f"Gate {'WARN' if gate == 'warn' else 'pass'}: a row warns when its earned-premium curve is more than "
        f"{pct(thr, 0)} of lifetime away from actual claim emergence at any term decile and the gap is larger "
        "than sampling noise (1.36 / sqrt(claims), the 95% Kolmogorov-Smirnov band). A/E above 1 in a band means "
        "losses outran earned premium there, so loss ratios in that part of the term are overstated.",
        cols=["Category", "Subcategory", "New/used", "Method", "Claims", "Max gap", "At decile", "Noise",
              "Best-fitting method", "Its gap", "Result"],
        rows=[[r.category_code, r.subcategory_code, r.new_used, r.method.replace("_", " "), num(r.claims),
               pct(r.max_abs_gap), num(r.worst_decile), pct(r.noise_band), r.best_method.replace("_", " "),
               pct(r.best_method_gap), _status(r.status)] for r in head.itertuples()],
        table_open=True, wide=True,
        note="Table actuarial.earning_fit; worst gated row and the gate status are in actuarial.run_summary "
             "(earning_fit_*). Subcategory * is the family (all subcategories pooled).",
    ))
    return out


def available(con: duckdb.DuckDBPyConnection) -> bool:
    n = con.execute("select count(*) from information_schema.tables where table_schema = 'actuarial'").fetchone()[0]
    return n > 0


def build(con: duckdb.DuckDBPyConnection, out_dir: Path) -> list:
    from reports import charts
    from reports.build import Kpi, Page, Section, chart_section, money, num, pct

    if not available(con):
        return []
    ratio = lambda a, b: a / b if b else float("nan")  # noqa: E731

    vv = _df(con, "select * from actuarial.valuation_view")
    pdr = _df(con, "select * from actuarial.premium_deficiency")
    summ = dict(con.execute("select metric, value from actuarial.run_summary").fetchall())
    f = lambda k: float(summ.get(k, "nan"))  # noqa: E731
    ib = _df(con, "select * from actuarial.ibnr_by_cohort")
    fc = _df(con, "select * from actuarial.loss_ratio_forecast order by quarter_start")
    bt = _df(con, "select * from actuarial.forecast_backtest")
    assign = _df(con, "select * from actuarial.earning_method_assignment order by category_code, subcategory_code, "
                      "new_used")
    em = _df(con, """select category_code, new_used, window_days, sum(contracts_exposed) as exposed,
                            sum(early_claim_contracts) as early, sum(within_wait_period) as in_wait
                     from actuarial.early_claim_emergence group by all order by 1, 2, 3""")
    dealers = _df(con, "select * from actuarial.early_claim_dealer where window_days = 180 order by z_score desc")
    try:
        fit = _df(con, "select * from actuarial.earning_fit")
    except duckdb.Error:  # built before the earning-fit table existed
        fit = pd.DataFrame()

    # downloads (CSV so the page has no extra dependency)
    dl = out_dir / "downloads" / "actuarial"
    dl.mkdir(parents=True, exist_ok=True)
    for t in DOWNLOADS:
        if t == "earning_fit" and fit.empty:
            continue
        _df(con, f"select * from actuarial.{t}").to_csv(dl / f"{t}.csv", index=False)

    all_ib = ib[ib["segment"] == "ALL"]
    fut = fc[fc["kind"] == "forecast"]
    model = str(fc["selected_model"].iloc[0]) if len(fc) else "n/a"
    nxt = fut.iloc[0] if len(fut) else None
    vsc90 = em[(em["category_code"] == "VSC") & (em["window_days"] == 90)]
    flagged = dealers[dealers["is_flagged"]]
    val_date = pd.Timestamp(vv["valuation_date"].iloc[0])
    cut = val_date.strftime("%b %d, %Y").replace(" 0", " ")
    sel_all = vv[(vv["segment"] == "ALL") & (vv["method"] == "selected")].iloc[0]
    fin = finance_earned_reserve(con, int(sel_all["valuation_period_key"]))
    tie = (f"; finance earned claims reserve {money(fin)} ({pct(abs(ratio(sel_all['earned_premium'] - fin, fin)), 1)}"
           " apart)") if fin else ""
    allb = finance_all_buckets(con, int(sel_all["valuation_period_key"]))
    all_earned = (f"; all cost-split buckets earned (the 'Earned premium' on the Finance and P&L pages): "
                  f"{money(allb[0])}") if allb else ""
    ibnr_total = all_ib["selected_ibnr"].sum()
    pdr_all = pdr[pdr["segment"] == "ALL"].iloc[0]
    all_upr = (f"; for context, the all-bucket UPR (finance net written less earned) is {money(allb[1])}"
               f" and unexpired losses are {pct(ratio(all_ib['unexpired_risk_losses'].sum(), allb[1]))} of it"
               ) if allb else ""
    kpis = [
        Kpi("Earned claims reserve, ITD", money(sel_all["earned_premium"]),
            f"claims-reserve slice of dealer cost (the premium reserves are measured against), selected method, "
            f"to {cut} (valuation){tie}{all_earned}"),
        Kpi("Loss ratio, ITD", pct(sel_all["loss_ratio"]),
            f"incurred / earned claims reserve, both cut at {cut} (finance's loss ratio divides by all buckets)"),
        Kpi("IBNR", money(ibnr_total),
            f"incurred, not yet reported: {num(f('ibnr_days_of_incurred'), 1)} days of incurred losses; "
            f"claims first reported in the {num(f('ibnr_runoff_window_days'))} days after valuation already bring "
            f"{money(f('ibnr_runoff_new_claims'))} ({pct(f('ibnr_runoff_ratio'))} of it, against "
            f"{pct(f('ibnr_runoff_expected_share'))} expected by now; information, not a gate)"),
        Kpi("Losses on unexpired coverage (not IBNR)", money(all_ib["unexpired_risk_losses"].sum()),
            f"still to happen; {pct(pdr_all['unexpired_to_upr'])} of the {money(pdr_all['unearned_premium'])} "
            f"unearned claims reserve (UPR, claims slice){all_upr}",
            "critical" if pdr["is_deficient"].any() else "good"),
        Kpi("Next-quarter loss ratio", pct(nxt["forecast"]) if nxt is not None else "n/a",
            f"{pct(nxt['lower'])} to {pct(nxt['upper'])} ({model.replace('_', ' ')})" if nxt is not None else ""),
        Kpi("VSC 90-day early-claim rate", pct(ratio(vsc90["early"].sum(), vsc90["exposed"].sum())),
            f"{num(len(flagged))} dealers flagged at 180 days"),
    ]

    cats = sorted(c for c in vv["segment"].unique() if c != "ALL")
    methods = [m for m in METHOD_LABELS if m in set(vv["method"])]

    def lr(cat: str, m: str) -> float:
        r = vv[(vv["segment"] == cat) & (vv["method"] == m)]
        return ratio(r["incurred_losses"].sum(), r["earned_premium"].sum())

    s1 = chart_section(
        "Same claims, five earning methods",
        "Loss ratio = claims / earned claims reserve (the claims slice of dealer cost). The claims never change; "
        "only how fast premium counts as earned. "
        "The selected method uses a curve per product and new/used, set in actuarial/earning_methods.csv; "
        "the earning-fit charts below test each one against actual claim emergence. "
        f"Premium and claims are both cut at the valuation date, {cut}.",
        charts.bars([c.title() for c in cats], {METHOD_LABELS[m]: [lr(c, m) for c in cats] for m in methods},
                    yfmt=".0%", hover=".1%"),
        "act-methods", ["Category", *[METHOD_LABELS[m] for m in methods]],
        [[c.title(), *[pct(lr(c, m)) for m in methods]] for c in cats], wide=True,
    )
    s2 = Section(
        "Which earning method each product uses",
        "Most specific rule wins. Custom curves are piecewise-linear knots in actuarial/earning_curves.csv.",
        cols=["Category", "Subcategory", "New/used", "Method", "Curve", "Contracts", "Miles / month", "Why"],
        rows=[[r.category_code, r.subcategory_code, r.new_used, r.method.replace("_", " "), r.curve_id or "",
               num(r.contracts), num(r.avg_monthly_miles), r.rationale] for r in assign.itertuples()],
        table_open=True, wide=True,
    )
    seg = ib.groupby("segment", as_index=False)[
        ["reported_to_date", "selected_ibnr", "unexpired_risk_losses", "selected_ultimate", "ultimate_premium"]].sum()
    seg["ulr"] = seg["selected_ultimate"] / seg["ultimate_premium"]
    seg = seg.merge(pdr.drop(columns="unexpired_risk_losses"), on="segment", how="left")
    seg = pd.concat([seg[seg["segment"] != "ALL"], seg[seg["segment"] == "ALL"]])
    exp_pct = pct(float(pdr["maintenance_expense_ratio"].iloc[0]), 0) if len(pdr) else "n/a"
    s3 = Section(
        "Reserves by product category, with the premium-deficiency test",
        "Chain-ladder on sale-quarter cohorts gives each cohort's life-of-contract ultimate, so "
        "Reported + IBNR + Losses on unexpired coverage = Ultimate. Only a small part of the gap is IBNR. Most of "
        "it is claims on coverage that has not run yet. Those belong with the unearned claims reserve (UPR, the "
        "claims slice of dealer cost), so each "
        f"segment's UPR is tested against them plus {exp_pct} claims-handling expense: if the cost is larger than "
        "the UPR, a premium-deficiency reserve (PDR) is needed. Premium here is net of cancellations, including "
        "those still expected on young cohorts. A segment younger than its longest term (Specialty, Maintenance) "
        "develops past its oldest age with a tail borrowed from a long-history segment at the same share of term, "
        "floored by its unexpired exposure (actuarial.development_factors, tail_source). The All row is the sum "
        "of the segments.",
        cols=["Segment", "Reported", "IBNR", "Losses on unexpired coverage", "Ultimate", "Ultimate loss ratio",
              "UPR, claims slice (selected)", "Unexpired losses / UPR", "PDR"],
        rows=[[r.segment.title() if r.segment != "ALL" else {"text": "All", "cls": "strong"},
               money(r.reported_to_date), money(r.selected_ibnr), money(r.unexpired_risk_losses),
               money(r.selected_ultimate), pct(r.ulr), money(r.unearned_premium), pct(r.unexpired_to_upr),
               {"status": "critical", "text": money(r.premium_deficiency_reserve)} if r.is_deficient
               else {"status": "good", "text": "none needed"}] for r in seg.itertuples()],
        table_open=True, wide=True,
        note=f"UPR is the selected-method balance of the claims-reserve slice at {cut}; the all-bucket UPR on the "
             "finance pages also holds admin fee, commission, insurer fee and the other cost-split buckets"
             + (f" ({money(allb[1])} in all)" if allb else "") + ". The expense loading is an assumption "
             "(Settings.maintenance_expense_ratio), not fitted.",
    )
    short = summ.get("ibnr_runoff_flag") == "ibnr_short"
    s3b = Section(
        "Is the IBNR the right size? (back-test)",
        "IBNR is small because claims are reported fast: the mean report lag is "
        f"{num(f('report_lag_mean_days'), 1)} days, {pct(f('report_lag_share_within_14d'))} are reported within "
        f"2 weeks and {pct(f('report_lag_share_30d_plus'))} take 30 days or more. That makes IBNR about "
        f"{num(f('ibnr_days_of_incurred'), 1)} days of incurred losses "
        f"({pct(ratio(ibnr_total, f('reported_all')), 1)} of reported). The days after the valuation date "
        "check it, scored only on claims first reported after valuation (new claims are what IBNR stands for); "
        "development on claims already known is a case-reserve question and is shown apart, not netted in. "
        "A window this short can only see part of the report-lag tail, so the actual is also compared with the "
        f"{pct(f('ibnr_runoff_expected_share'))} of IBNR it should already show (E[min(lag, window)] / E[lag]). "
        + ("That A/E to date is above 1, so more has emerged than the window should bring. " if short else "")
        + "One quarter's emergence is a few dozen late, skewed claims, so these figures are information, not a "
        "pass/fail test; calibration is judged over the 12 earlier quarter-ends in the next table. "
        "Development factors use only "
        f"diagonals from {summ.get('factor_start_quarter', 'all')} on, when losses started posting on the report "
        "date; before that the first line posted about three weeks after the loss, which would inflate the age-1 "
        "factor that every open quarter is developed with.",
        cols=["Measure", "Amount"],
        rows=[["IBNR at valuation (selected)", money(ibnr_total)],
              [f"Claims first reported in the {num(f('ibnr_runoff_window_days'))} days after valuation "
               f"({num(f('ibnr_runoff_new_claim_count'))} claims)", money(f("ibnr_runoff_new_claims"))],
              ["New-claim A/E (new claims / IBNR)", pct(f("ibnr_runoff_ratio"))],
              ["Share of IBNR this window should already see", pct(f("ibnr_runoff_expected_share"))],
              ["A/E to date (new claims / expected so far)",
               num(f("ibnr_runoff_ae_to_date"), 2)],
              ["Case-reserve adequacy: development on claims known at valuation",
               money(f("case_development_known_claims"))],
              ["of which: reserves released on claims later denied", money(f("case_development_known_denied"))],
              ["of which: other (mostly settled below the reserve)", money(f("case_development_known_other"))],
              ["IBNR on unreported claims (claims per day x days of report lag x severity)",
               money(f("ibnr_unreported"))],
              ["IBNR on claims known at valuation (expected reserve releases and supplements)",
               money(f("ibnr_known_claim_development"))],
              ["Quarterly accident-triangle chain ladder (comparison only)",
               money(f("ibnr_chain_ladder_accident_quarter"))],
              ["Accident-quarter age-1 to age-2 factor", num(f("accident_link_age1"), 3)]],
        note=f"{pct(f('report_lag_share_30d_plus'))} of claims are reported 30 days or more after the loss, so "
             "new claims keep arriving after this window. "
             "See docs/ACTUARIAL.md section 6.",
    )
    try:
        ibt = _df(con, "select * from actuarial.ibnr_backtest order by valuation_date")
    except duckdb.Error:  # built before the IBNR back-test table existed
        ibt = pd.DataFrame()
    s3c = Section(
        "IBNR back-test over the last 12 quarter-ends",
        "The same IBNR method re-run at each earlier quarter-end with only the claims and lines known then, "
        "against the incurred that later posted on losses dated by it. The mean absolute error is "
        f"{money(f('ibnr_backtest_mae'))} on a mean realized {money(f('ibnr_backtest_mean_realized'))} "
        f"({pct(f('ibnr_backtest_scaled_mae'))}); in total the estimates are "
        f"{num(f('ibnr_backtest_bias'), 2)}x what was realized. Information only.",
        cols=["Quarter-end", "IBNR estimate", "of which unreported", "Realized after", "Error"],
        rows=[[r.valuation_quarter, money(r.ibnr_estimate), money(r.ibnr_unreported), money(r.realized),
               money(r.error)] for r in ibt.itertuples()][::-1],
    ) if len(ibt) else None
    line = fc[["quarter_start", "actual", "forecast", "lower", "upper"]].copy()
    s4 = chart_section(
        "Accident-quarter loss ratio and 4-quarter forecast",
        "Losses by the quarter they happened, developed to ultimate, over premium earned that quarter. The forecast "
        "model is the one with the lowest error when replayed on past quarters; the band is about 95% "
        "(1.96 x the backtest RMSE at that step, never narrower than at an earlier step).",
        charts.lines(line, "quarter_start", {"actual": "Actual (developed)", "forecast": "Forecast",
                                             "lower": "Lower band", "upper": "Upper band"}, yfmt=".0%", hover=".1%"),
        "act-forecast", ["Quarter", "Actual", "Forecast", "Lower", "Upper"],
        [[r.quarter, pct(r.actual), pct(r.forecast), pct(r.lower), pct(r.upper)] for r in fc.itertuples()][::-1],
        wide=True,
    )
    s5 = Section(
        "Forecast backtest (time-split, rolling origin)",
        "Each model is fitted on past quarters only and scored on the next four. Lowest mean absolute error wins.",
        cols=["Model", "Points", "Points by step", "MAE", "RMSE", "Bias", "Selected"],
        rows=[[r.model.replace("_", " "), num(r.points), getattr(r, "points_by_step", ""), pct(r.mae, 2),
               pct(r.rmse, 2), pct(r.bias, 2), "yes" if r.is_selected else ""] for r in bt.itertuples()],
        table_open=True,
    )
    vsc = em[em["category_code"] == "VSC"]
    windows = sorted(vsc["window_days"].unique())
    rate = {nu: [ratio(*vsc.loc[(vsc["new_used"] == nu) & (vsc["window_days"] == w), ["early", "exposed"]].sum())
                 for w in windows] for nu in sorted(vsc["new_used"].unique())}
    s6 = chart_section(
        "Early-claim emergence, VSC",
        "Share of contracts with a first claim within N days of the effective date (only contracts old enough to "
        "have finished the window count). Used vehicles claim earlier; new ones sit behind the factory warranty.",
        charts.bars([f"{w} days" for w in windows], {nu.title(): v for nu, v in rate.items()}, yfmt=".1%",
                    hover=".2%"),
        "act-early", ["Category", "New/used", "Window", "Exposed", "Early claims", "Rate", "Inside waiting period"],
        [[r.category_code, r.new_used, f"{r.window_days} days", num(r.exposed), num(r.early),
          pct(ratio(r.early, r.exposed), 2), num(r.in_wait)] for r in em.itertuples()],
    )
    s7 = Section(
        "Dealer early-claim watchlist (180 days)",
        "Actual vs expected early claims, where expected uses each contract's own product and new/used rate. "
        "Flagged: at least 30 exposed contracts, A/E of 1.5 or more and a z-score of 2 or more.",
        cols=["Dealer", "Exposed", "Early claims", "Expected", "A/E", "z", "Flag"],
        rows=[[r.dealer_number, num(r.contracts_exposed), num(r.early_claim_contracts), num(r.expected_early_claims, 1),
               num(r.actual_to_expected, 2), num(r.z_score, 1),
               {"status": "critical", "text": "review"} if r.is_flagged else ""]
              for r in dealers.head(25).itertuples()],
        table_open=True, wide=True,
        note="Downloads: " + ", ".join(f"downloads/actuarial/{t}.csv" for t in DOWNLOADS),
    )
    return [Page("actuarial.html", NAV_LABEL,
                 "Earning methods, reserves, early-claim emergence and a backtested loss-ratio forecast.",
                 kpis, [s1, s2, *fit_sections(fit, summ), s3, s3b, *([s3c] if s3c else []), s4, s5, s6, s7])]
